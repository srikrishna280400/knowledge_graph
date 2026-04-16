import json
from pathlib import Path
from sqlalchemy import select, func
from app.models import SavedItem
from app.ingest.priority import infer_kind_priority
from app.ingest.truncate import build_prompt_content
from migration.config import PRIMARY, DATA_DIR
from migration.db_factory import new_session


OUT_PATH = DATA_DIR / "groq_queue_budget.jsonl"

MODEL = "openai/gpt-oss-120b"

# Set these two knobs:
MAX_ITEMS = 90        # change to 4940 when needed
BUDGET_USD = 9.00     # must be large enough or it will stop early

# Pricing + token heuristic
PRICE_IN_PER_1M = 0.15
PRICE_OUT_PER_1M = 0.60
CHARS_PER_TOKEN = 4.0
MAX_COMPLETION_TOKENS = 2493  # Groq max for gpt-oss-120b


SYSTEM_PROMPT = (
"""You are analyzing a piece of content that someone deemed worth saving—a breadcrumb
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

CONNECTION METADATA for each node:
- Type: [Question, Tool, Framework, Person, Example, Data, Quote, Problem]
- Emotion: [Curiosity, Urgency, Inspiration, Confusion, Excitement]
- Action potential: [Someday/Maybe, Active Project, Reference, Inspiration]
- Related to: [Suggest connections to other nodes that might exist in their graph]

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
"connections": [],
"quality_flags": {"content_thin": false, "access_blocked_or_paywalled": false, "extraction_suspect": false}

No reasoning, no extra text, no trailing commentary.

For every concepts[i].type, use ONLY one of:
Question, Tool, Framework, Person, Example, Data, Quote, Problem, Concept, Project, Organization, Event.""")


def estimate_cost_usd(input_chars: int) -> float:
    in_tokens = input_chars / CHARS_PER_TOKEN
    in_cost = (in_tokens / 1_000_000.0) * PRICE_IN_PER_1M
    out_cost = (MAX_COMPLETION_TOKENS / 1_000_000.0) * PRICE_OUT_PER_1M
    return in_cost + out_cost


def main():
    session = new_session(PRIMARY)

    q = (
        select(
            SavedItem.id,
            SavedItem.canonical_url,
            SavedItem.title,
            SavedItem.status,
            SavedItem.extracted_text,
            func.length(SavedItem.extracted_text).label("text_len"),
        )
        .where(SavedItem.status.in_(["crawled", "crawled_wayback", "title_only"]))
    )

    rows = session.execute(q).all()
    session.close()

    # Priority + length (for tie-break)
    enriched = []
    for (id_, url, title, status, text, text_len) in rows:
        url = (url or "").strip()
        title = (title or "").strip()
        kind, prio = infer_kind_priority(url, title)
        tl = int(text_len) if text_len is not None else 0
        enriched.append((prio, -tl, kind, id_, url, title, status, text, tl))

    # priority asc, length desc within priority
    enriched.sort(key=lambda x: (x[0], x[1]))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    spent = 0.0
    kept = 0

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for (prio, _neglen, kind, id_, url, title, status, text, tl) in enriched:
            # Hard cap (90 or 4940)
            if kept >= MAX_ITEMS:
                break

            # Truncate logic happens inside build_prompt_content()
            if status == "title_only":
                user_payload = (
                    f"TITLE: {title}\n"
                    f"CONTENT:\n(Article unavailable/removed; title-only.)"
                )
                debug = {"mode": "title_only", "text_len": tl, "sent_chars": len(user_payload)}
            else:
                user_payload, debug = build_prompt_content(title=title, extracted_text=text)

            user_content = f"URL: {url}\n{user_payload}".strip()

            # Budget logic
            input_chars = len(SYSTEM_PROMPT) + len(user_content)
            est = estimate_cost_usd(input_chars)
            if spent + est > BUDGET_USD:
                break

            payload = {
                "id": id_,
                "canonical_url": url,
                "source": (url or "").strip(),  # metadata only
                "kind": kind,
                "priority": prio,
                "model": MODEL,
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
                "estimated_cost_usd": round(est, 6),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "debug": debug,
            }

            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            spent += est
            kept += 1

    print(f"OK: queued={kept} estimated_spend_usd={spent:.4f} out={OUT_PATH}")


if __name__ == "__main__":
    main()
