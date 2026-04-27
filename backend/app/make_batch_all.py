import argparse
import json
from pathlib import Path
from sqlalchemy import select, case, text, func, desc, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from app.models import SavedItem
from typing import cast
from app.models import SavedItem
from app.ingest.title_from_url import title_from_url
from app.ingest.truncate import build_prompt_content
from app.make_groq_budget_queue import BUDGET_USD, MAX_COMPLETION_TOKENS, estimate_cost_usd
from migration.config import PRIMARY, make_timestamped_batch_path, get_db_path
from migration.db_factory import new_session, get_engine, build_sqlite_url, primary_mirror_session_scope, get_primary_mirror_engine
import os
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"

def load_local_env() -> dict[str, str]:
    parsed: dict[str, str] = {}
    if ENV_PATH.exists():
        raw = dotenv_values(ENV_PATH)
        for k, v in raw.items():
            if k and v is not None:
                parsed[k] = str(v).strip()
        load_dotenv(dotenv_path=ENV_PATH, override=True, encoding="utf-8")
        for k, v in parsed.items():
            os.environ[k] = v
    return parsed

LOCAL_ENV = load_local_env()

# KG_90_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg_90.sqlite"


SYSTEM_PROMPT = """You are analyzing a piece of content that someone deemed worth savingâ€”a breadcrumb
to a thought, idea, or curiosity they wanted to preserve.

Your role: Extract the CONCEPTUAL DNA of this content and map it as a multi-layered
knowledge tree.

CONTEXT:

This is building a "Cognitive Map"â€”a living, evolving visualization of someone's
intellectual landscape. Each saved item is a node in their extended mind. Your job
is to:

1. Identify WHY this might have mattered to them (not just what it's about)
2. Break down the content into a hierarchy of interconnected concepts
3. Create bridges between seemingly unrelated ideas

OUTPUT STRUCTURE:

ROOT NODE (The Core Idea):
- What is the primary concept/theme/question this content addresses?
- What intellectual need does it serve? (Learn, Solve, Explore, Remember, Create)

LAYER 1 (Domains/Contexts - 3-5 nodes):
- Broad fields this touches (Technology, Philosophy, Health, Creativity, etc.)
- The "room" in their mind palace where this belongs

LAYER 2 (Themes/Topics - 5-8 nodes per Layer 1):
- Specific subjects within each domain
- These are navigational waypoints

LAYER 3 (Concepts/Entities - 10-15 nodes total):
- Concrete ideas, frameworks, people, tools, questions mentioned
- Atomic units of knowledge that can connect to OTHER saved items

LAYER 4 (Micro-Insights - as many as exist):
- Specific quotes, data points, techniques, examples
- The "why I highlighted this" moments
- Actionable or memorable fragments

Rules:
- "target.canonical_url" MUST be a canonical URL string when you can infer it from the user's knowledge graph;
if you cannot know it, set it to "" (empty string).
- "hint_title" is optional helper text to identify the target if canonical_url is empty.
- "shared_signals" is an array of short strings (keywords/topics) explaining overlap.
- Keep 0-8 connection objects total.
- Do NOT invent specific URLs; use "" if unknown.

Remember: You're not just categorizing content. You're reconstructing the mental
fingerprint of why a human mind said "this mattersâ€”save it." Find the resonance,
not just the topic.

ALSO NOTE

Return a single JSON object with exactly these top-level keys:
root, domains, themes, concepts, micro_insights, connections, quality_flags.
Do not output the key summary.

If uncertain: use [] for arrays, root.intellectual_need="Learn", text fields "",
and all quality_flags false.

Output template (fill it in, keep keys exactly):
"root": {"label": "", "intellectual_need": "Learn", "why_saved": ""},
"domains": [],
"themes": [],
"concepts": [],
"micro_insights": [],
"quality_flags": {"content_thin": false, "access_blocked_or_paywalled": false, "extraction_suspect": false}

No reasoning, no extra text, no trailing commentary.

For every concepts[i].type, use ONLY one of:
Question, Tool, Framework, Person, Example, Data, Quote, Problem, Concept, Project, Organization, Event.
"""

# ---- output ----
DEFAULT_OUT_PATH = make_timestamped_batch_path(PRIMARY)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out", dest="out_path", help="Path to batch JSONL to create")
    p.add_argument("--limit", type=int, help="Max URLs to batch for this run")
    return p.parse_args()


# ---- truncate logic ----
MAX_CONTENT_CHARS = 18_000  # per-item truncation limit

# ignore truncate logic
IGNORE_TRUNCATE_FOR_GROQ = 0  # set to 1 to send full extracted_text/raw_text as-is

# ---- batching ----
TEMP_TOP_N = 1  # set to 100/200/etc when you want


# Higher priority first: crawled > crawled_wayback > title_only
STATUS_PRIORITY = case(
    (SavedItem.status == "crawled", 0),
    (SavedItem.status == "crawled_wayback", 1),
    (SavedItem.status == "title_only", 2),
    else_=99,
)


def build_user_content(it: SavedItem) -> str:
    url = (it.canonical_url or it.raw_url or "").strip()
    title = (it.title or "").strip()

    if not title and url:
        title = title_from_url(url)

    if it.status == "title_only":
        raw_text = "(Article unavailable/removed; title-only.)"
    else:
        raw_text = (it.extracted_text or "").strip() or "(Empty extracted_text.)"

    if IGNORE_TRUNCATE_FOR_GROQ == 1:
        prompt_text = raw_text
    else:
        prompt_text, _meta = build_prompt_content(
            title=title,
            extracted_text=raw_text,
            min_chars=1200,
            max_chars=MAX_CONTENT_CHARS,
            )

    return (
        f"URL: {url}\n"
        f"TITLE: {title}\n"
        f"CONTENT:\n{prompt_text}\n"
    ).strip()


def ensure_llm_batched_table(session):
    bind = session.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    table_ref = "llm_batched" if is_sqlite else "app.llm_batched"
    batched_at_type = "TIMESTAMPTZ"
    batched_at_default = "CURRENT_TIMESTAMP"

    session.execute(text(f"""
    CREATE TABLE IF NOT EXISTS {table_ref} (
        saved_item_id TEXT PRIMARY KEY,
        canonical_url TEXT,
        batch_file TEXT,
        batched_at {batched_at_type} DEFAULT {batched_at_default}
    )
    """))


def insert_llm_batched(session, saved_item_id: str, canonical_url: str, batch_file: str) -> None:
    bind = session.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    table_ref = "llm_batched" if is_sqlite else "app.llm_batched"

    if is_sqlite:
        session.execute(
            text(f"""
            INSERT INTO {table_ref} (saved_item_id, canonical_url, batch_file)
            VALUES (:sid, :url, :bf)
            ON CONFLICT(saved_item_id) DO UPDATE SET
                canonical_url = excluded.canonical_url,
                batch_file = excluded.batch_file
            """),
            {"sid": saved_item_id, "url": canonical_url, "bf": batch_file},
        )
    else:
        session.execute(
            text(f"""
            INSERT INTO {table_ref} (saved_item_id, canonical_url, batch_file)
            VALUES (:sid, :url, :bf)
            ON CONFLICT (saved_item_id) DO UPDATE SET
                canonical_url = EXCLUDED.canonical_url,
                batch_file = EXCLUDED.batch_file
            """),
            {"sid": saved_item_id, "url": canonical_url, "bf": batch_file},
        )


def sqlite_table_exists(session, table_name: str, schema: str = "main") -> bool:
    row = session.execute(
        text(f"""
            SELECT 1
            FROM {schema}.sqlite_master
            WHERE type = 'table' AND name = :name
            LIMIT 1
        """),
        {"name": table_name},
    ).first()
    return row is not None

def main():
    args = parse_args()
    effective_limit = args.limit if args.limit is not None else TEMP_TOP_N
    if effective_limit <= 0:
        raise ValueError("--limit must be > 0")
    
    enforce_budget = args.limit is None

    out_path = Path(args.out_path) if args.out_path else DEFAULT_OUT_PATH
    if not out_path.is_absolute():
        out_path = Path(__file__).resolve().parents[1] / out_path

    local_engine = create_engine(
        build_sqlite_url(get_db_path(PRIMARY)),
        future=True,
        pool_pre_ping=True,
    )
    session = Session(
        bind=local_engine,
        autoflush=False,
        autocommit=False,
        future=True,
        )

    mirror_engine = get_primary_mirror_engine()
    mirror_session = None
    if mirror_engine is not None:
        mirror_session = Session(
            bind=mirror_engine,
            autoflush=False,
            autocommit=False,
            future=True,
            )
        print(f"[mirror] connected via {mirror_engine.dialect.name}")
        try:
            ensure_llm_batched_table(mirror_session)
            mirror_session.commit()
        except Exception as e:
            mirror_session.rollback()
            print(f"⚠ mirror llm_batched ensure failed; local sqlite write kept: {e}")
    else:
        print("ⓘ mirror disabled: no primary mirror DB configured")

    try:
        llm_processed_exists = sqlite_table_exists(session, "llm_processed")

        if not llm_processed_exists:
            print("ⓘ llm_processed table missing -> skipping processed-items exclusion")

        stmt = (
            select(SavedItem)
            .where(SavedItem.status.in_(["crawled", "crawled_wayback"]))
        )

        if llm_processed_exists:
            stmt = stmt.where(
                text(
                    "NOT EXISTS (SELECT 1 FROM llm_processed p WHERE p.saved_item_id = saved_items.id)"
                )
            )

        stmt = (
            stmt.where(
                text(
                    "NOT EXISTS (SELECT 1 FROM llm_batched b WHERE b.saved_item_id = saved_items.id)"
                )
            )
            .order_by(
                STATUS_PRIORITY,
                desc(func.length(func.coalesce(SavedItem.extracted_text, ""))),
                SavedItem.id,
            )
            .limit(effective_limit)
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)

        written = 0
        spent_est_usd = 0.0

        with out_path.open("w", encoding="utf-8") as f:
            for it in session.execute(stmt).scalars().all():
                user_content = build_user_content(it)
                input_chars = len(SYSTEM_PROMPT) + len(user_content)
                est = estimate_cost_usd(input_chars)

                if enforce_budget and written > 0 and (spent_est_usd + est) > BUDGET_USD:
                    break

                url = (it.canonical_url or it.raw_url or "").strip()
                content_mode = "title_only" if it.status == "title_only" else "full"

                payload = {
                    "id": it.id,
                    "canonical_url": (it.canonical_url or "").strip(),
                    "source": (it.raw_url or "").strip(),
                    "status": it.status,
                    "max_completion_tokens": MAX_COMPLETION_TOKENS,
                    "estimated_cost_usd": round(est, 6),
                    "content_mode": content_mode,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                }

                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

                insert_llm_batched(session, it.id, url, str(out_path))
                session.commit()

                written += 1
                spent_est_usd += est

                if mirror_session is not None:
                    try:
                        insert_llm_batched(mirror_session, it.id, url, str(out_path))
                        mirror_session.commit()
                    except Exception as e:
                        mirror_session.rollback()
                        print(f"⚠ mirror llm_batched sync failed; local sqlite write kept: {e}")

        print(f"OK: wrote {written} items to {out_path}")
        print(
            f"Estimated cost (cap={BUDGET_USD}): ${spent_est_usd:.4f} "
            f"(max_completion_tokens={MAX_COMPLETION_TOKENS})"
        )
    finally:
        session.close()
        if mirror_session is not None:
            mirror_session.close()


if __name__ == "__main__":
    main()

