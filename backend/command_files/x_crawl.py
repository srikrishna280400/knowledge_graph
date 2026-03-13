import time
import re
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, create_engine, or_
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright, Page, BrowserContext

from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url


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

DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"
engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)

PROFILE_DIR = Path(r"D:\playwright-x-profile")


# -------------------- core crawl --------------------

def process_one_x(session, page: Page) -> str | None:
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

        item.title = post_text.split("\n")[0][:120]
        item.extracted_text = combined_text
        item.status = "crawled"

        session.commit()
        return f"OK X crawled: {clean_url}"

    except Exception as e:
        item.status = "crawl_failed"
        session.commit()
        return f"ERROR X crawl_failed: {clean_url} ({e})"


# -------------------- runner --------------------

def main():
    session = SessionLocal()

    with sync_playwright() as p:
        context: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        page: Page = context.new_page()
        n = 0

        while True:
            msg = process_one_x(session, page)
            if msg is None:
                break
            print(msg)
            n += 1
            time.sleep(8)  # human-like delay

        context.close()

    session.close()
    print(f"DONE X: processed={n}")


if __name__ == "__main__":
    main()
