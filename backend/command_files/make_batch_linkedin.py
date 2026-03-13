import json
import time
from pathlib import Path
from sqlalchemy import select, case, text
from backend.app.db import SessionLocal
from backend.app.models import SavedItem
from backend.app.ingest.title_from_url import title_from_url
from backend.app.ingest.truncate import build_prompt_content

DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"
KG_90_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg_90.sqlite"

from backend.app.make_groq_budget_queue import BUDGET_USD, MAX_COMPLETION_TOKENS, estimate_cost_usd

SYSTEM_PROMPT = """
You are analyzing a piece of content that someone deemed worth saving—a breadcrumb
to a thought, idea, or curiosity they wanted to preserve.

Your role: Extract the CONCEPTUAL DNA of this content and map it as a multi-layered
knowledge tree.

CONTEXT:

This is building a "Cognitive Map"—a living, evolving visualization of someone's
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
fingerprint of why a human mind said "this matters—save it." Find the resonance,
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

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / f"groq_batch_linkedin_{int(time.time())}.jsonl"

MAX_CONTENT_CHARS = 18_000

def truncate_text(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 50].rstrip() + "\n\n[TRUNCATED]"

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
        
    prompt_text, _ = build_prompt_content(
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

def main():
    session = SessionLocal()
    session.execute(text(f"ATTACH DATABASE '{KG_90_PATH}' AS kg90"))

    stmt = (
        select(SavedItem)
        .where(SavedItem.status.in_(["crawled", "crawled_wayback", "title_only"]))
        .where(
            SavedItem.raw_url.contains("linkedin.com")
        )
        .where(text("NOT EXISTS (SELECT 1 FROM llm_processed p WHERE p.saved_item_id = saved_items.id)"))
        .where(text("NOT EXISTS (SELECT 1 FROM kg90.saved_items s90 WHERE s90.canonical_url = saved_items.canonical_url)"))
        .order_by(STATUS_PRIORITY, SavedItem.id)
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    spent_est_usd = 0.0

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for it in session.execute(stmt).scalars().all():
            user_content = build_user_content(it)

            input_chars = len(SYSTEM_PROMPT) + len(user_content)
            est = estimate_cost_usd(input_chars)

            if written > 0 and (spent_est_usd + est) > BUDGET_USD:
                break

            content_mode = "title_only" if it.status == "title_only" else "full"

            payload = {
                "id": it.id,
                "canonical_url": (it.canonical_url or "").strip(),
                "source": (it.raw_url or "").strip(), # ← NEW, METADATA ONLY
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
            written += 1
            spent_est_usd += est

    session.close()
    print(f"OK: wrote {written} LinkedIn items to {OUT_PATH}")
    print(f"Estimated cost: ${spent_est_usd:.4f}")

if __name__ == "__main__":
    main()
