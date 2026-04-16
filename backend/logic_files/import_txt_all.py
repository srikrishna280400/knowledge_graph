import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from dotenv import dotenv_values, load_dotenv
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from migration.config import PRIMARY, get_db_path, get_import_txt_path
from migration.db_factory import build_sqlite_url


BASE_DIR = Path(__file__).resolve().parents[2]
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
del LOCAL_ENV

TXT_PATH = get_import_txt_path(PRIMARY)
SQLITE_DB_PATH = get_db_path(PRIMARY)

START_AT = 0
TEMP_TOP_N = 2  # set to 1, 10, 100, etc. ; None = no limit

_LOCAL_ENGINE = None
_LOCAL_SESSIONMAKER = None
_MIRROR_ENGINE = None
_MIRROR_SESSIONMAKER = None


def get_local_sessionmaker():
    global _LOCAL_ENGINE, _LOCAL_SESSIONMAKER

    if _LOCAL_SESSIONMAKER is None:
        _LOCAL_ENGINE = create_engine(
            build_sqlite_url(SQLITE_DB_PATH),
            future=True,
            pool_pre_ping=True,
        )
        _LOCAL_SESSIONMAKER = sessionmaker(
            bind=_LOCAL_ENGINE,
            autoflush=False,
            autocommit=False,
            future=True,
        )
    return _LOCAL_SESSIONMAKER


def get_mirror_sessionmaker():
    global _MIRROR_ENGINE, _MIRROR_SESSIONMAKER

    mirror_url = (
        os.getenv("DATABASE_URL", "").strip()
        or os.getenv("KG_PRIMARY_DATABASE_URL", "").strip()
    )
    if not mirror_url:
        return None

    if _MIRROR_SESSIONMAKER is None:
        _MIRROR_ENGINE = create_engine(
            mirror_url,
            future=True,
            pool_pre_ping=True,
            connect_args={"prepare_threshold": None},
            )
        _MIRROR_SESSIONMAKER = sessionmaker(
            bind=_MIRROR_ENGINE,
            autoflush=False,
            autocommit=False,
            future=True,
        )
    return _MIRROR_SESSIONMAKER


@contextmanager
def mirror_session_scope():
    maker = get_mirror_sessionmaker()
    if maker is None:
        yield None
        return

    session = maker()
    try:
        try:
            search_path = session.execute(text("show search_path")).scalar()
            print(f"[mirror] search_path = {search_path}")
        except Exception:
            pass
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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


def iter_urls(path: Path):
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith("http://") or line.startswith("https://")):
            continue
        yield line


def resolve_single_profile_id(session) -> str:
    is_sqlite = session.bind.dialect.name == "sqlite"
    table_ref = "profiles" if is_sqlite else "app.profiles"

    rows = session.execute(
        text(f"SELECT id FROM {table_ref} ORDER BY created_at ASC LIMIT 2")
    ).fetchall()

    if not rows:
        raise RuntimeError(f"No profiles found in {table_ref}")

    if len(rows) > 1:
        raise RuntimeError(
            f"Multiple profiles found in {table_ref}; "
            "import_txt_all expects exactly one current profile"
        )

    return rows[0][0]


def ensure_profile_exists(session, profile_id: str) -> None:
    is_sqlite = session.bind.dialect.name == "sqlite"
    table_ref = "profiles" if is_sqlite else "app.profiles"

    exists = session.execute(
        text(f"SELECT 1 FROM {table_ref} WHERE id = :id LIMIT 1"),
        {"id": profile_id},
    ).scalar()

    if not exists:
        raise RuntimeError(f"Profile {profile_id} not found in {table_ref}")


def upsert_saved_item_on_session(
    session,
    raw_url: str,
    profile_id: str,
    *,
    forced_id: Optional[str] = None,
    canonical_url_override: Optional[str] = None,
    url_source_override: Optional[str] = None,
    status_override: Optional[str] = None,
):
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return None, "invalid"

    if not (raw_url.startswith("http://") or raw_url.startswith("https://")):
        return None, "invalid"

    canon = (canonical_url_override or canonicalize_url(raw_url) or "").strip()
    if not canon:
        return None, "invalid"

    source = (url_source_override or detect_url_source(raw_url) or "generic").strip()
    status_value = (status_override or "pending_crawl").strip()
    is_sqlite = session.bind.dialect.name == "sqlite"

    if is_sqlite:
        existing = session.execute(
            select(SavedItem).where(
                SavedItem.profile_id == profile_id,
                SavedItem.canonical_url == canon,
            )
        ).scalar_one_or_none()

        if existing:
            changed = False
            if not getattr(existing, "url_source", None):
                existing.url_source = source
                changed = True
            if not getattr(existing, "raw_url", None):
                existing.raw_url = raw_url
                changed = True
            if changed:
                session.flush()
            return existing.id, "existing"

        obj = SavedItem(
            profile_id=profile_id,
            raw_url=raw_url,
            canonical_url=canon,
            url_source=source,
            status=status_value,
        )
        if forced_id:
            obj.id = forced_id

        session.add(obj)
        session.flush()
        return obj.id, "inserted"

    existing = session.execute(
        text(
            """
            SELECT id, raw_url, url_source
            FROM app.saved_items
            WHERE profile_id = :profile_id
              AND canonical_url = :canonical_url
            LIMIT 1
            """
        ),
        {"profile_id": profile_id, "canonical_url": canon},
    ).mappings().first()

    if existing:
        update_needed = False
        params = {"id": existing["id"]}

        set_parts = []
        if not existing.get("url_source"):
            set_parts.append("url_source = :url_source")
            params["url_source"] = source
            update_needed = True

        if not existing.get("raw_url"):
            set_parts.append("raw_url = :raw_url")
            params["raw_url"] = raw_url
            update_needed = True

        if update_needed:
            session.execute(
                text(
                    f"""
                    UPDATE app.saved_items
                    SET {", ".join(set_parts)}
                    WHERE id = :id
                    """
                ),
                params,
            )
            session.flush()

        return existing["id"], "existing"

    new_id = forced_id or canon

    session.execute(
        text(
            """
            INSERT INTO app.saved_items
                (id, profile_id, raw_url, canonical_url, url_source, status)
            VALUES
                (:id, :profile_id, :raw_url, :canonical_url, :url_source, :status)
            """
        ),
        {
            "id": new_id,
            "profile_id": profile_id,
            "raw_url": raw_url,
            "canonical_url": canon,
            "url_source": source,
            "status": status_value,
        },
    )
    session.flush()
    return new_id, "inserted"

def get_existing_canonical_urls(session, profile_id: str) -> set[str]:
    is_sqlite = session.bind.dialect.name == "sqlite"
    table_ref = "saved_items" if is_sqlite else "app.saved_items"

    rows = session.execute(
        text(
            f"""
            SELECT canonical_url
            FROM {table_ref}
            WHERE profile_id = :profile_id
              AND canonical_url IS NOT NULL
              AND TRIM(canonical_url) <> ''
            """
        ),
        {"profile_id": profile_id},
    ).fetchall()

    return {str(r[0]).strip() for r in rows if r[0]}


def build_unseen_url_batch(local_session, mirror_session, profile_id: str):
    existing = set()
    existing |= get_existing_canonical_urls(local_session, profile_id)

    if mirror_session is not None:
        existing |= get_existing_canonical_urls(mirror_session, profile_id)

    chosen = []
    seen_in_txt = set()

    for line_no, raw_url in enumerate(iter_urls(TXT_PATH), start=1):
        if line_no <= START_AT:
            continue

        canon = (canonicalize_url(raw_url) or "").strip()
        if not canon:
            continue

        key = (profile_id, canon)
        if key in seen_in_txt:
            continue
        seen_in_txt.add(key)

        if canon in existing:
            continue

        chosen.append((raw_url, canon))

        if TEMP_TOP_N is not None and len(chosen) >= TEMP_TOP_N:
            break

    return chosen


def main():
    if not TXT_PATH.exists():
        raise FileNotFoundError(f"Missing {TXT_PATH}")

    if not SQLITE_DB_PATH.exists():
        raise FileNotFoundError(f"Missing DB {SQLITE_DB_PATH}")

    LocalSession = get_local_sessionmaker()
    local_session = LocalSession()

    total_seen = 0
    candidate_unique = 0
    inserted_local = 0
    skipped_local = 0
    inserted_mirror = 0
    skipped_mirror = 0

    try:
        local_profile_id = resolve_single_profile_id(local_session)

        with mirror_session_scope() as mirror_session:
            if mirror_session is not None:
                ensure_profile_exists(mirror_session, local_profile_id)

            for _ in iter_urls(TXT_PATH):
                total_seen += 1

            batch = build_unseen_url_batch(local_session, mirror_session, local_profile_id)
            candidate_unique = len(batch)

            for raw_url, canon in batch:
                item_id, local_status = upsert_saved_item_on_session(
                    local_session,
                    raw_url,
                    local_profile_id,
                    canonical_url_override=canon,
                )
                local_session.commit()

                if local_status == "existing":
                    skipped_local += 1
                elif local_status == "inserted":
                    inserted_local += 1

                if mirror_session is not None and local_status in ("inserted", "existing"):
                    _, mirror_status = upsert_saved_item_on_session(
                        mirror_session,
                        raw_url,
                        local_profile_id,
                        forced_id=item_id,
                        canonical_url_override=canon,
                    )
                    mirror_session.commit()

                    if mirror_status == "existing":
                        skipped_mirror += 1
                    elif mirror_status == "inserted":
                        inserted_mirror += 1
    finally:
        local_session.close()

    print(
        f"OK total_seen={total_seen} unique_candidates_selected={candidate_unique} "
        f"local_inserted={inserted_local} local_existing={skipped_local} "
        f"mirror_inserted={inserted_mirror} mirror_existing={skipped_mirror}"
    )


if __name__ == "__main__":
    main()