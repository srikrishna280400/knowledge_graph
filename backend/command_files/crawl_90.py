import time
import httpx
from sqlalchemy import select, create_engine
from sqlalchemy.orm import sessionmaker
from trafilatura import extract
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url
from app.ingest.priority import infer_kind_priority
from bs4 import BeautifulSoup
from urllib.parse import urlsplit

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

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

DB90_PATH = r"D:\My Docs\Product Management\P4\Working Code\max spohisticated\backend\data\kg_90.sqlite"
engine = create_engine(f"sqlite:///{DB90_PATH}")
SessionLocal = sessionmaker(bind=engine)

def process_one(session, client: httpx.Client) -> str | None:
    items = session.execute(
        select(SavedItem).where(
            SavedItem.status.in_([
                "pending_crawl",
                "crawl_failed",
                "extraction_failed",
            ])
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

    clean_url = canonicalize_url(item.raw_url)
    item.canonical_url = clean_url

    if is_youtube_url(clean_url):
        html = client.get(clean_url).text
        yt_title = extract_youtube_title(html)
        if yt_title:
            item.title = yt_title
        item.extracted_text = None
        item.status = "title_only"
        session.commit()
        return f"YOUTUBE TITLE_ONLY: {clean_url}"

    r = client.get(clean_url)
    if r.status_code >= 400:
        item.title = title_from_url(clean_url)
        item.extracted_text = None
        item.status = "title_only"
        session.commit()
        return f"TITLE_ONLY: {clean_url}"

    text = extract(r.text)
    if not text or len(text.strip()) < 200:
        item.title = title_from_url(clean_url)
        item.extracted_text = None
        item.status = "title_only"
        session.commit()
        return f"TITLE_ONLY: {clean_url}"

    item.extracted_text = text
    item.status = "crawled"
    session.commit()
    return f"OK crawled: {clean_url} chars={len(text)}"

def main():
    session = SessionLocal()
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=25.0) as client:
        n = 0
        while True:
            msg = process_one(session, client)
            if msg is None:
                break
            n += 1
            print(msg)
            time.sleep(1.0)
    session.close()
    print(f"DONE: processed={n}")

if __name__ == "__main__":
    main()
