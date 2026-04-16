from sqlalchemy import select
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from pathlib import Path
from migration.config import KG_90, get_db_path, get_import_txt_path
from migration.db_factory import new_session

TXT_PATH = get_import_txt_path(KG_90)
DB90_PATH = get_db_path(KG_90)



def iter_urls(path: Path):
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith("http://") or line.startswith("https://")):
            continue
        yield line


def main():
    if not TXT_PATH.exists():
        raise FileNotFoundError(f"Missing: {TXT_PATH}")
    if not DB90_PATH.exists():
        raise FileNotFoundError(f"Missing DB: {DB90_PATH}")

    session = new_session(KG_90)

    total_lines = 0
    valid_urls = 0
    inserted = 0
    skipped_existing = 0

    for raw_url in iter_urls(TXT_PATH):
        total_lines += 1
        valid_urls += 1
        canon = canonicalize_url(raw_url)

        exists = session.scalar(
            select(SavedItem.id).where(SavedItem.canonical_url == canon)
        )
        if exists:
            skipped_existing += 1
            continue

        session.add(
            SavedItem(raw_url=raw_url, canonical_url=canon, status="pending_crawl")
        )
        inserted += 1

    session.commit()
    session.close()

    print(
        f"OK: read={total_lines} valid={valid_urls} inserted={inserted} dup_skipped={skipped_existing}"
    )


if __name__ == "__main__":
    main()
