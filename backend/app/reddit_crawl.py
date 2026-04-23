import time
import re
from pathlib import Path
from urllib.parse import urlsplit
from sqlalchemy import select, create_engine, text
from playwright.sync_api import sync_playwright, Page, BrowserContext
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url
from migration.config import PRIMARY
from migration.db_factory import new_session, get_engine
import argparse
from sqlalchemy.orm import sessionmaker
import os
from contextlib import contextmanager 
from dotenv import load_dotenv, dotenv_values
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


PROFILE_DIR = Path(r"D:\playwright-reddit-profile")
MIN_MEANINGFUL_CHARS = 0
HUMAN_DELAY = 3
MAX_COMMENTS = 200

# =========================
# HELPERS
# =========================

def is_live_reddit_broken(
    page: Page,
    response_status: int | None,
    title: str,
    post_text: str,
    comments_text: str,
) -> tuple[bool, str]:
    """Wayback only if live Reddit is truly broken or empty."""
    try:
        body_text = strip_reddit_noise(page.locator("body").inner_text(timeout=3000))
    except Exception:
        body_text = ""

    lowered = body_text.lower()

    broken_markers = [
        "page not found",
        "error 404",
        "404",
        "server error",
        "something went wrong",
        "sorry, nobody on reddit goes by that name",
        "this page is no longer available",
        "request failed",
        "bad gateway",
        "gateway timeout",
        "service unavailable",
    ]

    if response_status is not None and response_status >= 400:
        return True, f"http_{response_status}"

    if any(marker in lowered for marker in broken_markers):
        if not (title or post_text or comments_text):
            return True, "broken_marker_and_no_content"

    if not (title or post_text or comments_text):
        if len(body_text.strip()) < 80:
            return True, "empty_page"

    return False, ""


def save_final_result(
    session,
    item_id: int,
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
            print(f"⚠ Mirror sync failed; local SQLite save kept: {e}")


def abandon_current_item(session):
    session.rollback()


def force_wayback(url: str) -> str:
    """Get Wayback Machine URL for most recent snapshot"""
    return f"https://web.archive.org/web/{url}"


# NEW:
def accept_nsfw_if_present(page: Page):
    """Click 'I'm over 18' button if present - AGGRESSIVE"""
    try:
        # Wait longer for modal to appear
        page.wait_for_timeout(2500)
        
        # Try multiple button selectors with more variations
        selectors = [
            "button:has-text(\"Yes I'm over 18\")",
            "button:has-text(\"I'm over 18\")",
            "button:has-text('Yes')",
            "button:has-text('Continue')",
            "button:has-text('Confirm')",
            "[id*='NSFW'] button",
            "[class*='nsfw'] button",
            "button[id*='over18']",
            "//button[contains(., '18')]",  # XPath fallback
        ]
        
        clicked = False
        for sel in selectors:
            try:
                btn = page.locator(sel)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=3000, force=True)  # Force click
                    page.wait_for_timeout(2500)
                    print("  ✓ Clicked NSFW button")
                    clicked = True
                    break
            except Exception:
                continue
        
        if not clicked:
            print("  ⓘ No NSFW prompt found")
            
    except Exception as e:
        print(f"  ⚠ NSFW handler error: {e}")

def kill_all_videos(page: Page):
    """
    HARD stop all video/audio execution.
    Prevents autoplay, buffering, JS stalls.
    """
    try:
        page.evaluate(
            """
            () => {
                // Pause and remove all videos
                document.querySelectorAll("video, audio").forEach(v => {
                    try {
                        v.pause();
                        v.muted = true;
                        v.currentTime = 0;
                        v.removeAttribute("src");
                        v.load();
                        // Also try to remove from DOM
                        // v.remove();
                    } catch(e) {}
                });
                
                // Kill Reddit's shreddit-player components
                document.querySelectorAll("shreddit-player").forEach(p => {
                    try {
                        p.remove();
                    } catch(e) {}
                });
                
                // Stop any media playing
                if (window.HTMLMediaElement && window.HTMLMediaElement.prototype.play) {
                    const origPlay = window.HTMLMediaElement.prototype.play;
                    window.HTMLMediaElement.prototype.play = function() { 
                        this.pause();
                        return Promise.resolve(); 
                    };
                }
            }
            """
        )
        print("  ✓ Videos killed")
    except Exception as e:
        print(f"  Failed to kill videos: {e}")


def scroll_to_load_comments(page: Page):
    """Scroll down to trigger lazy-loaded comments"""
    try:
        for i in range(5):  # Scroll 5 times
            page.evaluate("window.scrollBy(0, 800)")
            page.wait_for_timeout(500)
        
        # Scroll back to top
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        print("  ✓ Scrolled to load comments")
    except Exception as e:
        print(f"  Scroll failed: {e}")


def wait_for_page_loaded(page: Page, timeout: int = 30000):
    """
    Wait until page is fully loaded - network idle + DOM stable
    """
    try:
        # Wait for domcontentloaded first
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
        print("  ✓ DOM loaded")
        
        # Wait a bit for dynamic content
        page.wait_for_timeout(3000)
        
        # Try to wait for network idle (but don't fail if timeout)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
            print("  ✓ Network idle")
        except Exception:
            print("  ⚠ Network still active (continuing anyway)")
            
    except Exception as e:
        print(f"  ⚠ Page load wait failed: {e}")


def is_video_or_image_post(page: Page) -> bool:
    """Detect if post is video/image content"""
    try:
        # Check for video elements
        if page.locator("video").count() > 0:
            return True
        if page.locator("shreddit-player").count() > 0:
            return True
        if page.locator("div[data-testid='media-container']").count() > 0:
            return True
        
        # Check for image posts
        if page.locator("img[alt*='Post image']").count() > 0:
            return True
        if page.locator("div[data-click-id='image']").count() > 0:
            return True
            
        return False
    except Exception:
        return False


def strip_reddit_noise(text: str) -> str:
    """Remove Reddit UI noise from extracted text"""
    if not text:
        return ""

    lines = []
    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        # Skip common UI elements
        if line.lower() in {
            "reply", "share", "save", "hide", "report",
            "give award", "award", "crosspost", "follow",
            "sort by", "best", "new", "top", "controversial", "old", "q&a",
            "view discussions in", "more posts from",
            "single comment thread", "view all comments",
            "continue this thread", "load more comments",
            "show replies", "more replies",
        }:
            continue

        # Skip vote counts
        if re.match(r"^\d+\s+(upvotes?|downvotes?|points?|votes?)$", line.lower()):
            continue
        
        # Skip time stamps like "5 hours ago"
        if re.match(r"^\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago$", line.lower()):
            continue
        
        # Skip single-character lines
        if len(line) == 1:
            continue
        
        # Skip just usernames (u/something)
        if re.match(r"^u/\w+$", line):
            continue

        lines.append(line)

    cleaned = "\n".join(lines)
    # Remove excessive newlines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def is_meaningful(text: str) -> bool:
    """Check if text has ANY content"""
    return bool(text and len(text.strip()) > 0)  # Just check if not empty


# =========================
# EXTRACTION
# =========================

def extract_post_title(page: Page) -> str:
    """Extract post title"""
    selectors = [
        "shreddit-post h1",
        "h1[slot='title']",
        "div[data-test-id='post-content'] h1",
        "h1",
        "[data-adclicklocation='title'] h3",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count():
                title = strip_reddit_noise(loc.inner_text(timeout=3000))
                if title:
                    print(f"  ✓ Title found ({len(title)} chars): {title[:60]}...")
                    return title
        except Exception:
            continue
    
    print("  ⚠ No title found")
    return ""


def extract_post_text(page: Page) -> str:
    """Extract post body text"""
    selectors = [
        "div[slot='text-body']",
        "shreddit-post div[slot='text-body']",
        "div[data-test-id='post-content'] div[data-click-id='text']",
        "div.usertext-body",
        "div[data-testid='post-container'] p",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count():
                text = strip_reddit_noise(loc.inner_text(timeout=3000))
                if text and len(text) > 20:
                    print(f"  ✓ Post body found ({len(text)} chars)")
                    return text
        except Exception:
            continue

    print("  ⚠ No post body found (may be link/media post)")
    return ""


def extract_all_comments(page: Page) -> str:
    """Extract all comment text - AGGRESSIVE approach"""
    texts = []

    # Try EVERY possible selector for Reddit comments
    selectors = [
        "shreddit-comment",  # New Reddit
        "div[id^='t1_']",  # Old Reddit comment IDs
        "div.Comment",  # Old Reddit class
        "div[data-testid='comment']",
        "div[data-test-id='comment']",
        "[thing-type='comment']",
        "div.entry",  # Old Reddit entry
    ]

    found_selector = None
    max_found = 0

    # Find which selector gives us the most comments
    for sel in selectors:
        try:
            count = page.locator(sel).count()
            if count > max_found:
                max_found = count
                found_selector = sel
        except Exception:
            continue

    if not found_selector:
        print("  ❌ NO COMMENTS FOUND with any selector")
        return ""

    print(f"  ✓ Found {max_found} comments using selector: {found_selector}")

    # Extract comments using best selector
    try:
        locs = page.locator(found_selector)
        count = locs.count()
        
        for i in range(min(count, MAX_COMMENTS)):
            try:
                comment_text = locs.nth(i).inner_text(timeout=3000)
                cleaned = strip_reddit_noise(comment_text)
                
                # Only keep if has substantial content
                if cleaned and len(cleaned) > 15:
                    texts.append(cleaned)
                    
            except Exception as e:
                print(f"  ⚠ Failed to extract comment {i}: {e}")
                continue

        print(f"  ✓ Extracted {len(texts)} meaningful comments")
        
    except Exception as e:
        print(f"  ❌ Comment extraction failed: {e}")

    return "\n\n---COMMENT---\n\n".join(texts)


# =========================
# CORE PROCESSOR
# =========================

def process_one_reddit(session, page: Page, mirror_session=None) -> str | None:
    session.rollback()

    item = (
        session.execute(
            select(SavedItem)
            .where(
                SavedItem.status.in_([
                    "pending_crawl",
                    "crawl_failed",
                    "extraction_failed",
                    "title_only",
                ])
            )
            .where(
                (SavedItem.raw_url.contains("reddit.com"))
                | (SavedItem.raw_url.contains("redd.it"))
            )
            .order_by(SavedItem.id)
            .limit(1)
        )
        .scalar_one_or_none()
    )

    if not item:
        return None

    item_id = item.id
    raw_url = item.raw_url
    original_url = canonicalize_url(raw_url)

    session.expunge(item)

    title = ""

    print(f"{'='*60}")
    print(f"Processing: {original_url}")
    print(f"{'='*60}")

    try:
        # =====================
        # 1️⃣ TRY LIVE REDDIT
        # =====================
        try:
            print("🌐 Attempting live Reddit...")
            response = page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
            response_status = response.status if response else None

            wait_for_page_loaded(page)
            accept_nsfw_if_present(page)
            page.wait_for_timeout(1000)
            accept_nsfw_if_present(page)
            accept_nsfw_if_present(page)

            kill_all_videos(page)
            page.wait_for_timeout(1000)
            kill_all_videos(page)

            scroll_to_load_comments(page)
            kill_all_videos(page)

            time.sleep(HUMAN_DELAY)

            is_media = is_video_or_image_post(page)
            print(f" 📊 Media post: {is_media}")
            print(f" 📊 HTTP status: {response_status}")

            title = extract_post_title(page)
            post_text = extract_post_text(page)

            print(" 🔍 Extracting comments...")
            comments_text = extract_all_comments(page)

            broken, broken_reason = is_live_reddit_broken(
                page,
                response_status,
                title,
                post_text,
                comments_text,
            )

            if broken:
                print(f"❌ Live Reddit broken: {broken_reason}")
                print(" 🔄 Trying Wayback Machine...")
                raise RuntimeError(f"live_reddit_broken:{broken_reason}")

            parts = []
            if title:
                parts.append(f"TITLE:{title}")
            if post_text:
                parts.append(f"POST:{post_text}")
            if comments_text:
                parts.append(f"COMMENTS:{comments_text}")

            combined = "".join(parts)

            print(f"📏 Total extracted: {len(combined)} chars")
            print(f" - Title: {len(title)} chars")
            print(f" - Post: {len(post_text)} chars")
            print(f" - Comments: {len(comments_text)} chars")

            if title or post_text or comments_text:
                final_title = title or title_from_url(original_url)

                save_final_result(
                    session,
                    item_id,
                    canonical_url=original_url,
                    title=final_title,
                    extracted_text=combined,
                    status="crawled",
                    summary=None,
                    mirror_session=mirror_session,
                )

                print(f"✅ SUCCESS - Saved {len(combined)} chars")
                return f"✅ OK reddit crawled (live): {original_url}"

            print(" ⚠️ Live page had no usable extracted content, trying Wayback...")

        except KeyboardInterrupt:
            abandon_current_item(session)
            print("⚠️ Interrupted mid-item. Leaving item untouched.")
            raise

        except Exception as e:
            abandon_current_item(session)
            print(f"❌ Live Reddit failed: {e}")
            print(" 🔄 Trying Wayback Machine...")

        # =====================
        # 2️⃣ TRY WAYBACK MACHINE
        # =====================
        try:
            wb_url = force_wayback(original_url)
            print(f"🕰️ Wayback URL: {wb_url}")

            page.goto(wb_url, wait_until="domcontentloaded", timeout=60000)

            wait_for_page_loaded(page, timeout=45000)
            kill_all_videos(page)
            scroll_to_load_comments(page)
            kill_all_videos(page)

            time.sleep(HUMAN_DELAY)

            wb_title = extract_post_title(page)
            wb_post = extract_post_text(page)
            wb_comments = extract_all_comments(page)

            wb_parts = []
            if wb_title:
                wb_parts.append(f"TITLE:{wb_title}")
            if wb_post:
                wb_parts.append(f"POST:{wb_post}")
            if wb_comments:
                wb_parts.append(f"COMMENTS:{wb_comments}")

            wb_combined = "".join(wb_parts)

            print(f" 📏 Wayback extracted: {len(wb_combined)} chars")

            if wb_title or wb_post or wb_comments:
                final_title = wb_title or title_from_url(original_url)

                save_final_result(
                    session,
                    item_id,
                    canonical_url=original_url,
                    title=final_title,
                    extracted_text=wb_combined,
                    status="crawled_wayback",
                    summary=None,
                    mirror_session=mirror_session,
                )

                print(f"✅ SUCCESS via Wayback - Saved {len(wb_combined)} chars")
                return f"✅ OK reddit crawled (wayback): {original_url}"

            print("⚠️ Wayback also insufficient...")

        except KeyboardInterrupt:
            abandon_current_item(session)
            print("⚠️ Interrupted mid-item. Leaving item untouched.")
            raise

        except Exception as e:
            abandon_current_item(session)
            print(f"❌ Wayback failed: {e}")

        # =====================
        # 3️⃣ FINAL FALLBACK
        # =====================
        print(" ⚠️ Falling back to title-only")

        save_final_result(
            session,
            item_id,
            canonical_url=original_url,
            title=title or title_from_url(original_url),
            extracted_text=None,
            status="title_only",
            summary="Reddit post unavailable (broken live page and wayback failed)",
            mirror_session=mirror_session,
        )

        return f"⚠️ TITLE_ONLY reddit: {original_url}"

    except KeyboardInterrupt:
        abandon_current_item(session)
        raise


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args()

# =========================
# MAIN LOOP
# =========================

def main():
    args = parse_args()
    limit = args.limit

    session = new_session(PRIMARY)

    with mirror_session_scope() as mirror_session, sync_playwright() as p:
        context = launch_social_context(p)
        page = context.new_page()

        page.set_default_navigation_timeout(60000)
        page.set_default_timeout(30000)

        n = 0
        while limit is None or n < limit:
            try:
                msg = process_one_reddit(session, page, mirror_session=mirror_session)
                if msg is None:
                    print("No more Reddit URLs to process")
                    break
                print(msg)
                n += 1
                time.sleep(HUMAN_DELAY)
            except KeyboardInterrupt:
                print("Interrupted by user")
                break
            except Exception as e:
                print(f"Error processing item: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5)
                continue

        context.close()

    session.close()
    print(f"\n{'='*60}")
    print(f"✅ DONE reddit crawl: processed={n} items limit={limit}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

