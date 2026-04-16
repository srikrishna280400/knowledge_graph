import argparse
import csv
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from importlib import import_module
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from .config import PRIMARY
from .db_factory import session_scope
from .db_schema import ensure_import_schema

canonicalize_url = import_module("app.ingest.canonicalize").canonicalize_url
SavedItem = import_module("app.models").SavedItem


URL_KEYS = (
    "url",
    "post_url",
    "postUrl",
    "link",
    "href",
    "canonical_url",
    "canonicalUrl",
)

def detect_url_source(url: str) -> str:
    host = (urlsplit(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "redd.it" or host.endswith("reddit.com"):
        return "reddit"
    if host.endswith("linkedin.com"):
        return "linkedin"
    if host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com"):
        return "x"
    return "generic"


def create_batch_run(
    session,
    *,
    profile_id: str | None,
    source_key: str,
    label: str,
    input_format: str,
    batch_id: str | None = None,
) -> str:
    batch_id = batch_id or str(uuid.uuid4())
    session.execute(text("""
    INSERT INTO extension_batch_runs (
        id, profile_id, source_key, label, input_format, status
    ) VALUES (
        :id, :profile_id, :source_key, :label, :input_format, 'running'
    )
    """), {
        "id": batch_id,
        "profile_id": profile_id,
        "source_key": source_key,
        "label": label,
        "input_format": input_format,
    })
    session.commit()
    return batch_id


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="Source key, e.g. linkedin_saved_post_hero")
    p.add_argument("--profile-id", dest="profile_id", required=True, help="Profile ID owner for this import")
    p.add_argument("--json-file", dest="json_file", help="Path to JSON export file")
    p.add_argument("--csv-file", dest="csv_file", help="Path to CSV export file")
    p.add_argument("--label", default="", help="Optional human label for this import batch")
    return p.parse_args()


def ensure_import_tables(session) -> None:
    session.execute(text("""
    CREATE TABLE IF NOT EXISTS extension_batch_runs (
        id TEXT PRIMARY KEY,
        profile_id TEXT,
        source_key TEXT NOT NULL,
        label TEXT,
        input_format TEXT NOT NULL,
        total_items INTEGER DEFAULT 0,
        new_saved_items INTEGER DEFAULT 0,
        existing_saved_items INTEGER DEFAULT 0,
        invalid_items INTEGER DEFAULT 0,
        status TEXT NOT NULL,
        error_text TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT
    )
    """))
    session.commit()


def _extract_url_from_dict(item: dict[str, Any]) -> str | None:
    for key in URL_KEYS:
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def iter_urls_from_payload(data: Any) -> Iterable[str]:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.strip():
                yield item.strip()
                continue
            if isinstance(item, dict):
                found = _extract_url_from_dict(item)
                if found:
                    yield found
        return

    if isinstance(data, dict):
        for top_key in ("urls", "items", "posts", "saved_posts", "savedPosts", "data", "results"):
            arr = data.get(top_key)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, str) and item.strip():
                        yield item.strip()
                        continue
                    if isinstance(item, dict):
                        found = _extract_url_from_dict(item)
                        if found:
                            yield found
                return


def iter_urls_from_json(path: Path) -> Iterable[str]:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    yield from iter_urls_from_payload(data)


def iter_urls_from_csv(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        candidate_keys = []
        for h in headers:
            hl = (h or "").strip()
            if hl in URL_KEYS or hl.lower() in URL_KEYS:
                candidate_keys.append(h)

        for row in reader:
            found = None

            for key in candidate_keys:
                v = row.get(key)
                if isinstance(v, str) and v.strip():
                    found = v.strip()
                    break

            if not found:
                for _, v in row.items():
                    if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                        found = v.strip()
                        break

            if found:
                yield found


def update_batch_run_done(
    session,
    *,
    batch_id: str,
    total_items: int,
    new_saved_items: int,
    existing_saved_items: int,
    invalid_items: int,
) -> None:
    session.execute(text("""
    UPDATE extension_batch_runs
    SET total_items = :total_items,
        new_saved_items = :new_saved_items,
        existing_saved_items = :existing_saved_items,
        invalid_items = :invalid_items,
        status = 'done',
        error_text = NULL,
        finished_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """), {
        "id": batch_id,
        "total_items": total_items,
        "new_saved_items": new_saved_items,
        "existing_saved_items": existing_saved_items,
        "invalid_items": invalid_items,
    })
    session.commit()


def mark_batch_run_failed(session, batch_id: str, error_text: str) -> None:
    session.execute(text("""
    UPDATE extension_batch_runs
    SET status = 'failed',
        error_text = :error_text,
        finished_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """), {
        "id": batch_id,
        "error_text": (error_text or "")[:2000],
    })
    session.commit()


def upsert_saved_item_on_session(
    session,
    raw_url: str,
    profile_id: str | None,
    *,
    forced_id: str | None = None,
):
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return None, "invalid"

    if not raw_url.startswith(("http://", "https://")):
        return None, "invalid"

    canon = canonicalize_url(raw_url)
    if not canon:
        return None, "invalid"

    source = detect_url_source(raw_url)

    existing = session.execute(
        select(SavedItem).where(
            SavedItem.profile_id == profile_id,
            SavedItem.canonical_url == canon,
        )
    ).scalar_one_or_none()

    if existing:
        if not getattr(existing, "url_source", None):
            existing.url_source = source
            session.flush()
        return existing.id, "existing"

    obj = SavedItem(
        profile_id=profile_id,
        raw_url=raw_url,
        canonical_url=canon,
        url_source=source,
        status="pending_crawl",
    )
    if forced_id:
        obj.id = forced_id

    session.add(obj)
    session.flush()
    return obj.id, "inserted"


def upsert_saved_item(session, raw_url: str, profile_id: str | None):
    return upsert_saved_item_on_session(session, raw_url, profile_id)


def ensure_profile_exists(session, profile_id: str | None) -> None:
    if not profile_id:
        raise RuntimeError("profile_id is required")

    exists = session.execute(
        text("SELECT 1 FROM profiles WHERE id = :id LIMIT 1"),
        {"id": profile_id},
    ).scalar()

    if not exists:
        raise RuntimeError(f"Unknown profile_id: {profile_id}")


def _run_import(
    session,
    *,
    profile_id: str | None,
    source_key: str,
    label: str,
    input_format: str,
    url_iter: Iterable[str],
) -> dict:
    ensure_import_schema(session)

    batch_id = create_batch_run(
        session,
        profile_id=profile_id,
        source_key=source_key,
        label=label,
        input_format=input_format,
    )

    total_items = 0
    new_saved_items = 0
    existing_saved_items = 0
    invalid_items = 0

    try:
        for raw_url in url_iter:
            total_items += 1
            item_id, status = upsert_saved_item_on_session(session, raw_url, profile_id)
            session.commit()

            if status == "inserted":
                new_saved_items += 1
            elif status == "existing":
                existing_saved_items += 1
            else:
                invalid_items += 1

        update_batch_run_done(
            session,
            batch_id=batch_id,
            total_items=total_items,
            new_saved_items=new_saved_items,
            existing_saved_items=existing_saved_items,
            invalid_items=invalid_items,
        )

        return {
            "batch_id": batch_id,
            "profile_id": profile_id,
            "source_key": source_key,
            "input_format": input_format,
            "total_items": total_items,
            "new_saved_items": new_saved_items,
            "existing_saved_items": existing_saved_items,
            "invalid_items": invalid_items,
        }

    except Exception as exc:
        session.rollback()
        mark_batch_run_failed(session, batch_id, str(exc))

        raise


def run_import_payload(
    *,
    source_key: str,
    payload: Any,
    label: str = "",
    target: str = PRIMARY,
    profile_id: str | None = None,
) -> dict:
    with session_scope(target) as session:
        ensure_profile_exists(session, profile_id)

    return _run_import(
        session,
        profile_id=profile_id,
        source_key=source_key,
        label=label,
        input_format="json",
        url_iter=iter_urls_from_payload(payload),
    )


def run_import_file(
    *,
    source_key: str,
    input_path: Path,
    input_format: str,
    label: str = "",
    target: str = PRIMARY,
    profile_id: str | None = None,
) -> dict:
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path

    if not input_path.exists():
        raise FileNotFoundError(f"Missing input file: {input_path}")

    with session_scope(target) as session:
        ensure_profile_exists(session, profile_id)

        url_iter = iter_urls_from_json(input_path) if input_format == "json" else iter_urls_from_csv(input_path)
        return _run_import(
            session,
            profile_id=profile_id,
            source_key=source_key,
            label=label,
            input_format=input_format,
            url_iter=url_iter
        )


def main() -> None:
    args = parse_args()

    if not args.json_file and not args.csv_file:
        raise RuntimeError("Provide --json-file or --csv-file")

    if args.json_file and args.csv_file:
        raise RuntimeError("Provide only one of --json-file or --csv-file")

    input_path = Path(args.json_file or args.csv_file)
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path

    if not input_path.exists():
        raise FileNotFoundError(f"Missing input file: {input_path}")

    input_format = "json" if args.json_file else "csv"

    result = run_import_file(
    source_key=args.source,
    input_path=input_path,
    input_format=input_format,
    label=args.label,
    target=PRIMARY,
    profile_id=args.profile_id,
    )

    print(f"OK import batch: {result['batch_id']}")
    print(f"Source: {result['source_key']}")
    print(f"Total: {result['total_items']}")
    print(f"Inserted new saved_items: {result['new_saved_items']}")
    print(f"Already existing: {result['existing_saved_items']}")
    print(f"Invalid: {result['invalid_items']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
