import time
import httpx
from sqlalchemy import select, create_engine
from sqlalchemy.orm import sessionmaker
from trafilatura import extract, extract_metadata
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url
from app.ingest.priority import infer_kind_priority
from bs4 import BeautifulSoup
from urllib.parse import urlsplit

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


# def force_wayback_url(url: str) -> str:
#     return f"https://web.archive.org/{url}"


# def is_reddit_url(url: str) -> bool:
#     host = urlsplit(url).netloc.lower()
#     return "reddit.com" in host


def is_youtube_url(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }


def extract_youtube_title(html: str) -> str | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()
    except Exception:
        pass
    return None


DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"
engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)


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
        return None
    except Exception:
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


def set_title_only(item: SavedItem, url: str, session, reason: str):
    item.title = item.title or title_from_url(url)
    item.extracted_text = None
    item.summary = (
        "Article unavailable (removed/blocked). Showing URL-derived title only. "
        f"Reason: {reason}"
    )
    item.status = "title_only"
    session.commit()


def process_one(session, client: httpx.Client) -> str | None:
    items = session.execute(
        select(SavedItem).where(
            SavedItem.status.in_(["pending_crawl", "crawl_failed", "extraction_failed"]),
            ~SavedItem.raw_url.contains("reddit.com"),
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

    # Reddit → Wayback first
    # if is_reddit_url(fetch_url):
    #     fetch_url = force_wayback_url(fetch_url)

    item.canonical_url = fetch_url

    # YouTube unchanged
    if is_youtube_url(fetch_url):
        html = http_get_text(client, fetch_url)
        if html:
            yt_title = extract_youtube_title(html)
            if yt_title:
                item.title = yt_title
                item.extracted_text = None
                item.summary = "YouTube video (title-only extraction)."
                item.status = "title_only"
                session.commit()
                return f"YOUTUBE TITLE_ONLY: {fetch_url}"

        item.title = "YouTube Video"
        item.extracted_text = None
        item.summary = "YouTube video (title unavailable)."
        item.status = "title_only"
        session.commit()
        return f"YOUTUBE TITLE_ONLY (fallback): {fetch_url}"

    html = http_get_text(client, fetch_url)
    source = "direct"

    if not html:
        snap = wayback_closest_snapshot(client, fetch_url)
        if snap:
            html = http_get_text(client, snap)
            source = "wayback"

    # Reddit direct fallback (FINAL)
    # if not html and is_reddit_url(original_url):
    #     html = http_get_text(client, original_url)
    #     source = "reddit_direct"

    if not html:
        set_title_only(item, fetch_url, session, reason="download_failed")
        return f"TITLE_ONLY: {fetch_url}"

    apply_metadata_title(item, html)
    item.title = item.title or title_from_url(original_url)

    text = extract(html) or ""

    # 🔥 CRITICAL FIX:
    # Reddit direct crawls NEVER fall back to title_only if HTML exists
    if len(text.strip()) < 200:
        set_title_only(item, fetch_url, session, reason=f"extraction_failed_{source}")
        return f"TITLE_ONLY: {fetch_url}"

    item.extracted_text = text
    item.status = "crawled_wayback" if source == "wayback" else "crawled"
    session.commit()
    return f"OK crawled ({source}): {fetch_url} chars={len(text)}"


def main():
    session = SessionLocal()
    with httpx.Client(
        headers={"User-Agent": UA},
        follow_redirects=True,
        timeout=25.0,
    ) as client:
        n = 0
        while True:
            msg = process_one(session, client)
            if msg is None:
                break
            print(msg)
            n += 1
            time.sleep(1.0)
    session.close()
    print(f"DONE: processed={n}")


if __name__ == "__main__":
    main()
