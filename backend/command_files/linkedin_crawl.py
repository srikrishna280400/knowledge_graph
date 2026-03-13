import time
from pathlib import Path
from sqlalchemy import select, create_engine
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright, Page, BrowserContext
from backend.app.models import SavedItem
from backend.app.ingest.canonicalize import canonicalize_url


# ---- DB setup (IDENTICAL TO crawl_all.py) ----
DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"
engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)
# --------------------------------------------

PROFILE_DIR = Path(r"D:\playwright-linkedin-profile")


def process_one_linkedin(session, page: Page) -> str | None:
    item = session.execute(
        select(SavedItem).where(
            SavedItem.status.in_(["pending_crawl", "crawl_failed", "title_only"]),
            SavedItem.raw_url.contains("linkedin.com"),
        ).limit(1)
    ).scalar_one_or_none()

    if not item:
        return None

    clean_url = canonicalize_url(item.raw_url)
    item.canonical_url = clean_url

    try:
        page.goto(clean_url, wait_until="domcontentloaded")
        time.sleep(10)

        post = page.locator("div.feed-shared-update-v2").first
        text = post.inner_text(timeout=8000)

        item.title = text.split("\n")[0][:120]
        item.extracted_text = text
        item.status = "crawled"

        session.commit()
        return f"OK linkedin crawled: {clean_url}"

    except Exception as e:
        item.status = "crawl_failed"
        session.commit()
        return f"ERROR linkedin crawl_failed: {clean_url} ({e})"


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
            msg = process_one_linkedin(session, page)
            if msg is None:
                break
            print(msg)
            n += 1
            time.sleep(8)

        context.close()

    session.close()
    print(f"DONE linkedin: processed={n}")


if __name__ == "__main__":
    main()
