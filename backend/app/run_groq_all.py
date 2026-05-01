import json
import os
import httpx
from dotenv import load_dotenv, dotenv_values
from sqlalchemy import text, create_engine
import time
import csv
from app.ingest.priority import infer_kind_priority
from migration.config import PRIMARY, get_groq_run_paths, get_db_path
from migration.db_factory import get_engine, build_sqlite_url, get_primary_mirror_engine
from typing import Any
import argparse
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"


def load_local_env() -> dict[str, str]:
    parsed: dict[str, str] = {}

    if ENV_PATH.exists():
        raw = dotenv_values(ENV_PATH)

        for k, v in raw.items():
            if k and v is not None:
                parsed[k] = str(v).strip()

        # Force local .env to populate/override during local development.
        load_dotenv(dotenv_path=ENV_PATH, override=True, encoding="utf-8")

        # Extra safety: explicitly copy parsed keys into os.environ too.
        for k, v in parsed.items():
            os.environ[k] = v

    return parsed


LOCAL_ENV = load_local_env()


GROQ_PATHS = get_groq_run_paths(PRIMARY)
DEFAULT_IN_PATH = GROQ_PATHS["input"]
DEFAULT_OUT_PATH = GROQ_PATHS["output"]
DEFAULT_INCOMPLETE_OUT_PATH = GROQ_PATHS["incomplete"]
DEFAULT_CSV_OUT_PATH = GROQ_PATHS["csv"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", help="Specific batch JSONL to run")
    return p.parse_args()


def resolve_run_paths(in_path_arg: str | None) -> tuple[Path, Path, Path, Path]:
    if not in_path_arg:
        return (
            DEFAULT_IN_PATH,
            DEFAULT_OUT_PATH,
            DEFAULT_INCOMPLETE_OUT_PATH,
            DEFAULT_CSV_OUT_PATH,
        )

    in_path = Path(in_path_arg)
    if not in_path.is_absolute():
        in_path = BASE_DIR / in_path

    out_path = in_path.with_name(f"{in_path.stem}_outputs.jsonl")
    incomplete_out_path = in_path.with_name(f"{in_path.stem}_outputs_incomplete.jsonl")
    csv_out_path = in_path.with_name(f"{in_path.stem}_outputs.csv")

    return in_path, out_path, incomplete_out_path, csv_out_path


#SCHEMA_ENFORCED = True

DEFAULT_MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_MAX_COMPLETION_TOKENS = 2493

RESPONSE_FORMAT = {
  "type": "json_schema",
  "json_schema": {
    "name": "cognitive_map_rich_v1",
    "strict": False,
    "schema": {
      "type": "object",
      "additionalProperties": False,
      "properties": {
        "root": {
          "type": "object",
          "additionalProperties": False,
          "properties": {
            "label": {"type": "string"},
            "intellectual_need": {
              "type": "string",
              "enum": ["Learn", "Solve", "Explore", "Remember", "Create"]
            },
            "why_saved": {"type": "string"}
          },
          "required": ["label", "intellectual_need", "why_saved"]
        },

        "domains": {
          "type": "array",
          "maxItems": 8,
          "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
              "name": {"type": "string"},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "required": ["name", "confidence"]
          }
        },

        "themes": {
          "type": "array",
          "maxItems": 24,
          "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
              "name": {"type": "string"},
              "tier": {"type": "integer", "enum": [1, 2, 3, 4]},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "required": ["name", "tier", "confidence"]
          }
        },

        "concepts": {
          "type": "array",
          "maxItems": 30,
          "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
              "name": {"type": "string"},
              "type": {
                "type": "string",
                "enum": [
                  "Question", "Tool", "Framework", "Person", "Example", "Data",
                  "Quote", "Problem", "Concept", "Project", "Organization", "Event"
                ]
              },
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "required": ["name", "type", "confidence"]
          }
        },

        "micro_insights": {
          "type": "array",
          "maxItems": 30,
          "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
              "text": {"type": "string"},
              "type": {"type": "string", "enum": ["Quote", "Data", "Technique", "Example", "Aha"]},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1}
            },
            "required": ["text", "type", "confidence"]
          }
        },

        "quality_flags": {
          "type": "object",
          "additionalProperties": False,
          "properties": {
            "content_thin": {"type": "boolean"},
            "access_blocked_or_paywalled": {"type": "boolean"},
            "extraction_suspect": {"type": "boolean"}
          },
          "required": ["content_thin", "access_blocked_or_paywalled", "extraction_suspect"]
        }
      },

      "required": [
        "root", "domains", "themes", "concepts",
        "micro_insights", "quality_flags" #"connections"
      ]
    }
  }
}

def _safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None

def _postfill_defaults(parsed: Any) -> dict:
    if not isinstance(parsed, dict):
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
    is_sqlite = engine.dialect.name == "sqlite"

    created_at_type = "TEXT" if is_sqlite else "TIMESTAMPTZ"
    created_at_default = "CURRENT_TIMESTAMP"
    processed_at_type = "TEXT" if is_sqlite else "TIMESTAMPTZ"
    processed_at_value = "CURRENT_TIMESTAMP"

    with engine.begin() as conn:
        conn.execute(text(f"""
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
          created_at        {created_at_type} DEFAULT {created_at_default}
        );
        """))

        conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS llm_processed (
          saved_item_id TEXT PRIMARY KEY,
          processed_at  {processed_at_type} DEFAULT {processed_at_value}
        );
        """))


def save_to_db(engine, result: dict):
    saved_item_id = result.get("id")
    if not saved_item_id:
        return

    is_sqlite = engine.dialect.name == "sqlite"
    now_sql = "datetime('now')" if is_sqlite else "CURRENT_TIMESTAMP"

    parsed_json = (
        json.dumps(result.get("parsed"), ensure_ascii=False)
        if result.get("parsed") is not None
        else None
    )
    raw_json = (
        json.dumps(result.get("raw"), ensure_ascii=False)
        if isinstance(result.get("raw"), dict)
        else (result.get("raw") or "")
    )

    with engine.begin() as conn:
        if is_sqlite:
            conn.execute(text(f"""
                INSERT OR REPLACE INTO llm_outputs
                (saved_item_id, canonical_url, model, content, parsed_json, raw_response_json, http_status, attempts, content_mode, created_at)
                VALUES (:id, :url, :model, :content, :parsed, :raw, :http_status, :attempts, :content_mode, {now_sql})
            """), {
                "id": saved_item_id,
                "url": result.get("canonical_url"),
                "model": result.get("model"),
                "content": result.get("assistant_content"),
                "parsed": parsed_json,
                "raw": raw_json,
                "http_status": result.get("http_status"),
                "attempts": result.get("attempts"),
                "content_mode": result.get("content_mode"),
            })
        else:
            conn.execute(text(f"""
                INSERT INTO llm_outputs
                (saved_item_id, canonical_url, model, content, parsed_json, raw_response_json, http_status, attempts, content_mode, created_at)
                VALUES (:id, :url, :model, :content, :parsed, :raw, :http_status, :attempts, :content_mode, {now_sql})
                ON CONFLICT (saved_item_id) DO UPDATE SET
                    canonical_url = EXCLUDED.canonical_url,
                    model = EXCLUDED.model,
                    content = EXCLUDED.content,
                    parsed_json = EXCLUDED.parsed_json,
                    raw_response_json = EXCLUDED.raw_response_json,
                    http_status = EXCLUDED.http_status,
                    attempts = EXCLUDED.attempts,
                    content_mode = EXCLUDED.content_mode,
                    created_at = EXCLUDED.created_at
            """), {
                "id": saved_item_id,
                "url": result.get("canonical_url"),
                "model": result.get("model"),
                "content": result.get("assistant_content"),
                "parsed": parsed_json,
                "raw": raw_json,
                "http_status": result.get("http_status"),
                "attempts": result.get("attempts"),
                "content_mode": result.get("content_mode"),
            })

        if result.get("http_status") and result["http_status"] < 400 and result.get("parsed") is not None:
            if is_sqlite:
                conn.execute(text(f"""
                    INSERT OR REPLACE INTO llm_processed (saved_item_id, processed_at)
                    VALUES (:id, {now_sql})
                """), {"id": saved_item_id})
            else:
                conn.execute(text(f"""
                    INSERT INTO llm_processed (saved_item_id, processed_at)
                    VALUES (:id, {now_sql})
                    ON CONFLICT (saved_item_id) DO UPDATE SET
                        processed_at = EXCLUDED.processed_at
                """), {"id": saved_item_id})


headers: dict = {}
timeout: httpx.Timeout | None = None
engine = None

def main():
    args = parse_args()
    in_path, out_path, incomplete_out_path, csv_out_path = resolve_run_paths(args.in_path)

    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        groq_like_keys = sorted([k for k in LOCAL_ENV.keys() if "GROQ" in k or "API" in k])
        raise RuntimeError(
            f"Missing GROQ_API_KEY. "
            f"ENV_PATH={ENV_PATH} exists={ENV_PATH.exists()} "
            f"groq_like_keys_in_env_file={groq_like_keys}"
        )

    if not in_path.exists():
        raise FileNotFoundError(f"Missing input: {in_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    local_sqlite_engine = create_engine(
        build_sqlite_url(get_db_path(PRIMARY)),
        future=True,
        pool_pre_ping=True,
)
    mirror_engine = get_primary_mirror_engine()

    try:
        ensure_llm_tables(local_sqlite_engine)
        if mirror_engine is not None:
            try:
                ensure_llm_tables(mirror_engine)
            except Exception as exc:
                print("⚠ mirror ensure_llm_tables failed; local sqlite write kept ->", repr(exc))
                print("ensure_llm_tables: OK")
    except Exception as exc:
        print("ensure_llm_tables: FAILED ->", repr(exc))
        raise

    if mirror_engine is not None:
        with mirror_engine.begin() as conn:
            print("MIRROR DB DIALECT:", mirror_engine.dialect.name)

    with local_sqlite_engine.begin() as conn:
        tables = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ).fetchall()
        print("LOCAL SQLITE TABLES RIGHT AFTER ENSURE:", tables)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(60.0, read=180.0)

    interrupted = False

    with httpx.Client(headers=headers, timeout=timeout) as client, \
         in_path.open("r", encoding="utf-8") as fin, \
         out_path.open("w", encoding="utf-8") as fout, \
         csv_out_path.open("w", encoding="utf-8", newline="") as fcsv:

        jobs = []
        for line in fin:
            job = json.loads(line)

            url = job.get("canonical_url") or ""
            messages = job.get("messages") or []

            title_hint = ""
            if len(messages) >= 2:
                title_hint = (messages[1].get("content") or "")[:200]

            kind, priority = infer_kind_priority(url, title_hint)

            content_len = sum(
                len(m.get("content", "")) for m in messages
            )

            jobs.append((priority, -content_len, job))

        jobs.sort(key=lambda x: (x[0], x[1]))

        csv_writer = None

        try:
            for i, (_, _, job) in enumerate(jobs, start=1):
                model = job.get("model") or DEFAULT_MODEL
                max_comp = int(
                    job.get("max_completion_tokens")
                    or DEFAULT_MAX_COMPLETION_TOKENS
                )

                body = {
                    "model": model,
                    "messages": job.get("messages") or [],
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "n": 1,
                    "max_tokens": max_comp,
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
                        raw.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    result["assistant_content"] = content
                    parsed = _safe_json_loads(content) if content else {}
                    result["parsed"] = _postfill_defaults(parsed)
                    print(f"OK {i}: {job.get('canonical_url')}")
                else:
                    result["raw"] = r.text
                    result["error"] = "http_error"
                    print(f"FAIL {i}: status={r.status_code}")

                fout.write(json.dumps(result, ensure_ascii=False) + "\n")
                fout.flush()
                csv_writer.writerow(result)
                fcsv.flush()

                save_to_db(local_sqlite_engine, result)
                
                if mirror_engine is not None:
                    try:
                        save_to_db(mirror_engine, result)
                    
                    except Exception as e:
                        raise RuntimeError(f"mirror save_to_db failed for {result.get('id')}: {e}") from e

                time.sleep(2.2)

        except KeyboardInterrupt:
            interrupted = True
            fout.flush()
            fout.close()
            out_path.rename(incomplete_out_path)
            print(f"INTERRUPTED — partial results saved to {incomplete_out_path}")

    if not interrupted:
        print(f"DONE: wrote outputs to {out_path}")

if __name__ == "__main__":
    main()
