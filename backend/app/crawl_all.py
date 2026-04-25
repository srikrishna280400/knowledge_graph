import subprocess
import sys
import time
import os
from contextlib import contextmanager
from pathlib import Path
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv, dotenv_values
from sqlalchemy import select, func, or_, create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from trafilatura import extract, extract_metadata
from .models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url
from app.ingest.priority import infer_kind_priority
from migration.config import PRIMARY, get_db_path
from migration.db_factory import new_session, primary_mirror_session_scope, build_sqlite_url
import argparse


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"

def new_local_session() -> Session:
    local_engine = create_engine(
        build_sqlite_url(get_db_path(PRIMARY)),
        future=True,
        pool_pre_ping=True,
    )
    return Session(
        bind=local_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, help="Total URLs to crawl in this pass")
    p.add_argument("--reddit-limit", type=int, default=None)
    p.add_argument("--linkedin-limit", type=int, default=None)
    p.add_argument("--x-limit", type=int, default=None)
    p.add_argument("--generic-limit", type=int, default=None)
    return p.parse_args()

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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
RETRYABLE_STATUSES = ["pending_crawl", "crawl_failed", "extraction_failed"]

REDDIT_CRAWL_LIMIT = 1
LINKEDIN_CRAWL_LIMIT = 0
X_CRAWL_LIMIT = 0
GENERIC_CRAWL_LIMIT = 1

def save_final_result(
    session,
    item_id: str,
    *,
    canonical_url: str,
    title: str,
    extracted_text: str | None,
    status: str,
    summary: str | None = None,
    mirror_session=None,
):
    item = session.get(SavedItem, item_id)
    if not item:
        raise RuntimeError(f"SavedItem not found: {item_id}")

    item.canonical_url = canonical_url
    item.title = title
    item.extracted_text = extracted_text
    item.status = status
    item.summary = summary

    session.commit()
    session.expire_all()

    if mirror_session is not None:
        try:
            mirror_saved_item_by_id(session, mirror_session, item_id)
            mirror_session.commit()
        except Exception as e:
            mirror_session.rollback()
            print(f"⚠ mirror sync failed; local sqlite write kept: {e}")


def mirror_saved_item_by_id(local_session, mirror_session, saved_item_id: str) -> None:
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


def commit_and_mirror(local_session, item_id: str, mirror_session=None):
    local_session.commit()
    local_session.expire_all()
    if mirror_session is not None:
        try:
            mirror_saved_item_by_id(local_session, mirror_session, item_id)
            mirror_session.commit()
        except Exception as e:
            mirror_session.rollback()
            print(f"⚠ mirror sync failed; local sqlite write kept: {e}")


def pending_count(session, source: str) -> int:
    return session.scalar(
        select(func.count()).select_from(SavedItem).where(
            SavedItem.status.in_(RETRYABLE_STATUSES),
            SavedItem.url_source == source,
        )
    ) or 0


def pending_generic_count(session) -> int:
    return session.scalar(
        select(func.count()).select_from(SavedItem).where(
            SavedItem.status.in_(RETRYABLE_STATUSES),
            or_(
                SavedItem.url_source.is_(None),
                SavedItem.url_source.in_(["generic", "youtube", "web"]),
            ),
        )
    ) or 0


def run_script(module_name: str, limit: int | None = None):
    print(f"=== RUN {module_name} limit={limit} ===")
    cmd = [sys.executable, "-m", module_name]
    if limit is not None:
        cmd.extend(["--limit", str(limit)])
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{module_name} failed with exit code {result.returncode}")


def is_youtube_url(url: str) -> bool:
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc.lower()
    return host in {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "youtu.be", "www.youtu.be",
    }


def extract_youtube_title(html: str) -> str | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:title")
        if not og:
            return None
        content = og.get("content")
        if isinstance(content, list):
            content = content[0] if content else None
        if isinstance(content, str):
            content = content.strip()
        return content or None
    except Exception:
        return None


def http_get_text(client: httpx.Client, url: str) -> str | None:
    try:
        r = client.get(url)
        if r.status_code >= 400:
            return None
        return r.text
    except Exception:
        return None


def wayback_closest_snapshot(client: httpx.Client, url: str) -> str | None:
    api = "https://archive.org/wayback/available"
    try:
        r = client.get(api, params={"url": url})
        if r.status_code >= 400:
            return None
        data = r.json()
        closest = (data.get("archived_snapshots") or {}).get("closest") or {}
        if closest.get("available") is True and closest.get("url"):
            return str(closest["url"])
    except Exception:
        return None
    return None


def apply_metadata_title(item: SavedItem, html: str):
    try:
        meta = extract_metadata(html)
    except Exception:
        meta = None
    if not meta:
        return
    page_title = getattr(meta, "title", None)
    if page_title and len(page_title.strip()) >= 3:
        item.title = page_title.strip()

def set_title_only(item: SavedItem, url: str, session, reason: str, mirror_session=None):
    item.title = item.title or title_from_url(url)
    item.extracted_text = None
    item.summary = (
        "Article unavailable (removed/blocked). Showing URL-derived title only. "
        f"Reason: {reason}"
    )
    item.status = "title_only"
    commit_and_mirror(session, item.id, mirror_session)


def process_one_generic(session, client: httpx.Client, mirror_session=None) -> str | None:
    items = session.execute(
        select(SavedItem).where(
            SavedItem.status.in_(RETRYABLE_STATUSES),
            or_(
                SavedItem.url_source.is_(None),
                SavedItem.url_source.in_(["generic", "youtube", "web"]),
            ),
        )
    ).scalars().all()

    if not items:
        return None

    ranked = []
    for it in items:
        _, priority = infer_kind_priority(it.raw_url or "", it.title)
        ranked.append((priority, it.id, it))

    ranked.sort(key=lambda x: (x[0], x[1]))
    item = ranked[0][2]

    original_url = canonicalize_url(item.raw_url)
    fetch_url = original_url
    item.canonical_url = fetch_url

    if is_youtube_url(fetch_url):
        html = http_get_text(client, fetch_url)
        if html:
            yt_title = extract_youtube_title(html)
            if yt_title:
                item.title = yt_title
                item.extracted_text = None
                item.summary = "YouTube video (title-only extraction)."
                item.status = "title_only"
                commit_and_mirror(session, item.id, mirror_session)
                return f"YOUTUBE TITLE_ONLY: {fetch_url}"

        item.title = "YouTube Video"
        item.extracted_text = None
        item.summary = "YouTube video (title unavailable)."
        item.status = "title_only"
        commit_and_mirror(session, item.id, mirror_session)
        return f"YOUTUBE TITLE_ONLY fallback: {fetch_url}"

    html = http_get_text(client, fetch_url)
    source = "direct"

    if not html:
        snap = wayback_closest_snapshot(client, fetch_url)
        if snap:
            html = http_get_text(client, snap)
            source = "wayback"

    if not html:
        set_title_only(item, fetch_url, session, reason="download_failed", mirror_session=mirror_session)
        return f"TITLE_ONLY: {fetch_url}"

    apply_metadata_title(item, html)
    item.title = item.title or title_from_url(original_url)

    text = extract(html) or ""
    if len(text.strip()) < 200:
        set_title_only(item, fetch_url, session, reason=f"extraction_failed_{source}", mirror_session=mirror_session)
        return f"TITLE_ONLY: {fetch_url}"

    item.extracted_text = text
    item.summary = None
    item.status = "crawled_wayback" if source == "wayback" else "crawled"
    commit_and_mirror(session, item.id, mirror_session)
    
    return f"OK crawled ({source}): {fetch_url} chars={len(text.strip())}"


def run_generic_queue(limit: int | None = None):
    session = new_session(PRIMARY)
    try:
        with primary_mirror_session_scope() as mirror_session, httpx.Client(
            headers={"User-Agent": UA},
            follow_redirects=True,
            timeout=25.0,
        ) as client:
            n = 0
            while limit is None or n < limit:
                msg = process_one_generic(session, client, mirror_session=mirror_session)
                if msg is None:
                    break
                print(msg)
                n += 1
                time.sleep(1.0)
            print(f"DONE generic: processed={n}")
    finally:
        session.close()


def resolved_cap(cli_value: int | None, default_cap: int) -> int:
    return cli_value if cli_value is not None else default_cap


def take_quota(pending: int, cap: int, remaining: int | None) -> int:
    if cap <= 0:
        return 0
    if remaining is None:
        return min(pending, cap)
    if remaining <= 0:
        return 0
    return min(pending, cap, remaining)


def main():
    args = parse_args()
    remaining = args.limit

    reddit_cap = resolved_cap(args.reddit_limit, REDDIT_CRAWL_LIMIT)
    linkedin_cap = resolved_cap(args.linkedin_limit, LINKEDIN_CRAWL_LIMIT)
    x_cap = resolved_cap(args.x_limit, X_CRAWL_LIMIT)
    generic_cap = resolved_cap(args.generic_limit, GENERIC_CRAWL_LIMIT)

    session = new_local_session()
    try:
        reddit_n = pending_count(session, "reddit")
        linkedin_n = pending_count(session, "linkedin")
        x_n = pending_count(session, "x")
        generic_n = pending_generic_count(session)
    finally:
        session.close()

    if reddit_n == 0 and linkedin_n == 0 and x_n == 0 and generic_n == 0:
        print("DONE all: no pending items left")
        return

    reddit_take = take_quota(reddit_n, reddit_cap, remaining)
    if reddit_take > 0:
        run_script("app.reddit_crawl", reddit_take)
        if remaining is not None:
            remaining -= reddit_take

    linkedin_take = take_quota(linkedin_n, linkedin_cap, remaining)
    if linkedin_take > 0:
        run_script("app.linkedin_crawl", linkedin_take)
        if remaining is not None:
            remaining -= linkedin_take

    x_take = take_quota(x_n, x_cap, remaining)
    if x_take > 0:
        run_script("app.x_crawl", x_take)
        if remaining is not None:
            remaining -= x_take

    generic_take = take_quota(generic_n, generic_cap, remaining)
    if generic_take > 0:
        run_generic_queue(generic_take)
        if remaining is not None:
            remaining -= generic_take

    print("DONE crawl_all: one capped pass finished")
