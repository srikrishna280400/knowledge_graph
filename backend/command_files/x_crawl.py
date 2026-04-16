import time
import re
from pathlib import Path
from urllib.parse import urlsplit
from sqlalchemy import select, or_, create_engine, text
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright, Page, BrowserContext
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from migration.config import PRIMARY
from migration.db_factory import new_session
import argparse
import os
from dotenv import load_dotenv, dotenv_values
from contextlib import contextmanager
from app.browser_profile import launch_social_context


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

MIRROR_DATABASE_URL = (
    os.getenv("KGPRIMARYMIRROR_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or ""
).strip()

_MIRROR_ENGINE = None
_MIRROR_SESSIONMAKER = None


def normalize_pg_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def get_mirror_sessionmaker():
    global _MIRROR_ENGINE, _MIRROR_SESSIONMAKER

    if not MIRROR_DATABASE_URL:
        return None

    if _MIRROR_SESSIONMAKER is None:
        url = normalize_pg_url(MIRROR_DATABASE_URL)
        _MIRROR_ENGINE = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args={
                "prepare_threshold": None,
                "options": "-c search_path=app,public",
            },
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
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mirror_saved_item_by_id(local_session, mirror_session, saved_item_id):
    if mirror_session is None:
        return

    src = local_session.get(SavedItem, saved_item_id)
    if src is None:
        return

    cols = [c.name for c in SavedItem.__table__.columns]
    payload = {col: getattr(src, col) for col in cols}

    insert_cols = ", ".join(cols)
    insert_vals = ", ".join(f":{c}" for c in cols)
    update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "id")

    mirror_session.execute(
        text(f"""
            INSERT INTO app.saved_items ({insert_cols})
            VALUES ({insert_vals})
            ON CONFLICT (id) DO UPDATE SET
            {update_set}
        """),
        payload,
    )


def commit_and_mirror(local_session, item_id, mirror_session=None):
    local_session.commit()
    local_session.expire_all()

    if mirror_session is not None:
        try:
            mirror_saved_item_by_id(local_session, mirror_session, item_id)
            mirror_session.commit()
        except Exception as e:
            mirror_session.rollback()
            print(f"⚠ mirror sync failed; local sqlite write kept: {e}")

# -------------------- helpers --------------------

def strip_x_authors(text: str) -> str:
    if not text:
        return text

    lines = []
    for line in text.splitlines():
        line = line.strip()

        # remove @handles anywhere
        line = re.sub(r"@\w+", "", line)

        # drop obvious author / UI noise lines
        if (
            line.lower().startswith("replying to")
            or line.lower() == "follow"
            or line.lower().endswith("follow")
            or line in {"Â·", ""}
        ):
            continue

        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    return cleaned.strip()


def is_x_ad(text: str) -> bool:
    if not text:
        return False

    lowered = text.lower()
    return (
        "promoted" in lowered
        or "\nad\n" in f"\n{lowered}\n"
        or lowered.startswith("ad ")
        or "sponsored" in lowered
    )


def extract_likes(text: str) -> int:
    for line in text.split("\n"):
        if "Like" in line or "Likes" in line:
            digits = "".join(c for c in line if c.isdigit())
            if digits:
                return int(digits)
    return 0


def is_x_url(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


# -------------------- DB setup --------------------

# DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"
# engine = create_engine(f"sqlite:///{DB_PATH}")
# SessionLocal = sessionmaker(bind=engine)

PROFILE_DIR = Path(r"D:\playwright-x-profile")


# -------------------- core crawl --------------------

def process_one_x(session, page: Page, mirror_session=None) -> str | None:
    item = session.execute(
        select(SavedItem).where(
            SavedItem.status.in_(["pending_crawl", "crawl_failed", "title_only"]),
            or_(
                SavedItem.raw_url.contains("x.com"),
                SavedItem.raw_url.contains("twitter.com"),
            ),
        ).limit(1)
    ).scalar_one_or_none()

    if not item:
        return None

    clean_url = canonicalize_url(item.raw_url)
    item.canonical_url = clean_url

    try:
        page.goto(clean_url, wait_until="domcontentloaded")
        time.sleep(10)  # human dwell

        articles = page.locator("article").all()
        if not articles:
            raise RuntimeError("No X post articles found")

        # ---- main post ----
        post_text_raw = articles[0].inner_text(timeout=8000)
        post_text = strip_x_authors(post_text_raw)
        post_likes = extract_likes(post_text)

        less_than = None     # (likes, text)
        greater_than = None  # (likes, text)

        # ---- replies ----
        for reply in articles[1:]:
            raw = reply.inner_text(timeout=5000)

            if is_x_ad(raw):
                continue

            text = strip_x_authors(raw)
            likes = extract_likes(text)

            if likes < post_likes:
                if not less_than or likes > less_than[0]:
                    less_than = (likes, text)
            elif likes > post_likes:
                if not greater_than or likes > greater_than[0]:
                    greater_than = (likes, text)

        combined_text = (
            f"POST:\n{post_text}\n\n"
            f"--- TOP REPLY (LIKES < POST) ---\n"
            f"{less_than[1] if less_than else 'N/A'}\n\n"
            f"--- TOP REPLY (LIKES > POST) ---\n"
            f"{greater_than[1] if greater_than else 'N/A'}"
        )

        item.title = post_text.splitlines()[0][:120]
        item.extracted_text = combined_text
        item.summary = None
        item.status = "crawled"
        commit_and_mirror(session, item.id, mirror_session)
        
        return f"OK X crawled {clean_url}"

    except Exception as e:
        item.status = "crawl_failed"
        commit_and_mirror(session, item.id, mirror_session)
                
        return f"ERROR X crawl_failed: {clean_url} ({e})"

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args()

# -------------------- runner --------------------

def main():
    args = parse_args()
    limit = args.limit

    session = new_session(PRIMARY)
    with mirror_session_scope() as mirror_session, sync_playwright() as p:
        context = launch_social_context(p)
        page = context.new_page()

        n = 0
        while limit is None or n < limit:
            msg = process_one_x(session, page, mirror_session=mirror_session)
            if msg is None:
                break
            print(msg)
            n += 1
            time.sleep(8)

        context.close()
        session.close()

        print(f"DONE processed={n} limit={limit}")


if __name__ == "__main__":
    main()
