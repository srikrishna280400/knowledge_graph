import json
import os
import time
from pathlib import Path
import httpx
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from groq import Groq
from app.run_groq_all import RESPONSE_FORMAT
import csv
from typing import Any


load_dotenv()

DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"

IN_PATH = Path(__file__).resolve().parents[1] / "data" / "groq_batch_linkedin.jsonl"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "groq_batch_linkedin_outputs.jsonl"
INCOMPLETE_OUT_PATH = OUT_PATH.with_name(OUT_PATH.stem + "groq_batch_linkedin_incomplete.jsonl")
CSV_OUT_PATH = OUT_PATH.with_suffix(".csv")
#SCHEMA_ENFORCED = True


DEFAULT_MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MAX_COMPLETION_TOKENS = 2493


# ---------- helpers (UNCHANGED) ----------

def _safe_json_loads(s: str) -> dict[str, Any] | None:
    try:
        value = json.loads(s)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _postfill_defaults(parsed: dict[str, Any] | None) -> dict[str, Any]:
    if parsed is None:
        parsed = {}

    parsed.setdefault("root", {"label": "", "intellectual_need": "Learn", "why_saved": ""})
    parsed.setdefault("domains", [])
    parsed.setdefault("themes", [])
    parsed.setdefault("concepts", [])
    parsed.setdefault("micro_insights", [])
    parsed.setdefault(
        "quality_flags",
        {
            "content_thin": False,
            "access_blocked_or_paywalled": False,
            "extraction_suspect": False,
        },
    )
    return parsed

def ensure_llm_tables(engine):
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS llm_outputs (
          saved_item_id     TEXT PRIMARY KEY,
          canonical_url     TEXT,
          model             TEXT,
          content           TEXT,
          parsed_json       TEXT,
          raw_response_json TEXT,
          http_status       INTEGER,
          attempts          INTEGER,
          content_mode      TEXT,
          created_at        TEXT DEFAULT (datetime('now'))
        );
        """))
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS llm_processed (
          saved_item_id TEXT PRIMARY KEY,
          processed_at  TEXT
        );
        """))

def save_to_db(engine, result: dict):
    sid = result.get("id")
    if not sid:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT OR REPLACE INTO llm_outputs
            (saved_item_id, canonical_url, model, content, parsed_json, raw_response_json, http_status, attempts, content_mode, created_at)
            VALUES (:id, :url, :model, :content, :parsed, :raw, :http_status, :attempts, :content_mode, datetime('now'))
        """), {
            "id": sid,
            "url": result.get("canonical_url"),
            "model": result.get("model"),
            "content": result.get("assistant_content"),
            "parsed": json.dumps(result.get("parsed"), ensure_ascii=False) if result.get("parsed") else None,
            "raw": json.dumps(result.get("raw"), ensure_ascii=False) if isinstance(result.get("raw"), dict) else (result.get("raw") or ""),
            "http_status": result.get("http_status"),
            "attempts": result.get("attempts"),
            "content_mode": result.get("content_mode"),
        })

        if result.get("http_status") and result["http_status"] < 400 and result.get("parsed"):
            conn.execute(text("""
                INSERT OR REPLACE INTO llm_processed (saved_item_id, processed_at)
                VALUES (:id, datetime('now'))
            """), {"id": sid})

# ---------- main ----------

def main():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GROQ_API_KEY")

    if not IN_PATH.exists():
        raise FileNotFoundError(f"Missing input: {IN_PATH}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{DB_PATH}")
    ensure_llm_tables(engine)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(60.0, read=180.0)

    with httpx.Client(headers=headers, timeout=timeout) as client, \
         IN_PATH.open("r", encoding="utf-8") as fin, \
         OUT_PATH.open("w", encoding="utf-8") as fout, \
         CSV_OUT_PATH.open("w", encoding="utf-8", newline="") as fcsv:

        interrupted = False

        try:
            csv_writer = None

            for i, line in enumerate(fin, start=1):
                job = json.loads(line)

                model = job.get("model") or DEFAULT_MODEL
                max_comp = int(job.get("max_completion_tokens") or DEFAULT_MAX_COMPLETION_TOKENS)

                body = {
                    "model": model,
                    "messages": job.get("messages") or [],
                    "max_tokens": max_comp,
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "n": 1,
                    "stop": None,
                    "response_format": RESPONSE_FORMAT,
                }

                result = {
                    "id": job.get("id"),
                    "canonical_url": job.get("canonical_url"),
                    "source": job.get("source"),
                    "model": model,
                    "max_completion_tokens": max_comp,
                    "http_status": None,
                    "attempts": 0,
                    "raw": None,
                    "error": None,
                    "assistant_content": None,
                    "parsed": None,
                    "content_mode": job.get("content_mode"),
                }

                if csv_writer is None:
                    csv_writer = csv.DictWriter(
                        fcsv,
                        fieldnames=list(result.keys()),
                        extrasaction="ignore",
                    )
                    csv_writer.writeheader()

                max_attempts = 3
                backoff_s = 1.5
                r = None
                last_error_text = None

                for attempt in range(1, max_attempts + 1):
                    result["attempts"] = attempt
                    try:
                        r = client.post(URL, json=body)
                    except httpx.HTTPError as e:
                        last_error_text = str(e)
                        time.sleep(backoff_s * attempt)
                        continue

                    result["http_status"] = r.status_code
                    if r.status_code < 400:
                        break

                    last_error_text = r.text or ""

                    retryable = r.status_code in (429, 500, 502, 503)
                    if r.status_code == 400 and '"code":"json_validate_failed"' in last_error_text:
                        retryable = True

                    if retryable and attempt < max_attempts:
                        time.sleep(backoff_s * attempt)
                        continue
                    break

                if r is None:
                    result["error"] = last_error_text or "httpx_error"
                    result["raw"] = last_error_text
                elif r.status_code < 400:
                    raw = r.json()
                    result["raw"] = raw
                    content = (
                        (raw.get("choices", [{}])[0].get("message", {}) or {}).get("content")
                        or ""
                    )
                    result["assistant_content"] = content
                    parsed = _safe_json_loads(content) if content else {}
                    result["parsed"] = _postfill_defaults(parsed)
                    print(f"OK {i}: {job.get('canonical_url')}")
                else:
                    result["raw"] = r.text
                    result["error"] = "http_error"
                    print(f"FAIL {i}: status={r.status_code}")
                    print("ERROR BODY:", (r.text or "")[:2000])

                fout.write(json.dumps(result, ensure_ascii=False) + "\n")
                fout.flush()
                csv_writer.writerow(result)
                fcsv.flush()

                save_to_db(engine, result)
                time.sleep(2.2)

        except KeyboardInterrupt:
            interrupted = True
            fout.flush()
            OUT_PATH.rename(INCOMPLETE_OUT_PATH)
            print(f"\nINTERRUPTED — partial results saved to {INCOMPLETE_OUT_PATH}")

        if not interrupted:
            print(f"DONE: wrote outputs to {OUT_PATH}")

if __name__ == "__main__":
    main()
