from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
TXT_PATH = BASE_DIR / "data" / "MASTER_URL_LIST.txt"

DB_PATH = BASE_DIR / "data" / "kg.sqlite"
engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)

def iter_urls(path: Path):
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith("http://") or line.startswith("https://")):
            continue
        
        # TEMP: only Reddit links, change later
        if "reddit.com" not in line.lower():
            continue
        # yield only reddit for now

        yield line

def main():
    if not TXT_PATH.exists():
        raise FileNotFoundError(f"Missing: {TXT_PATH}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Missing DB: {DB_PATH}")

    session = SessionLocal()

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
