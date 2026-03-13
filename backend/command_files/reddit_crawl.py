import time
import re
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, create_engine
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright, Page, BrowserContext

from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url


# =========================
# CONFIG
# =========================

DB_PATH = r"D:\My Docs\Product Management\P4\Working Code\max sophisticated\backend\data\kg.sqlite"
PROFILE_DIR = Path(r"D:\playwright-reddit-profile")

MIN_MEANINGFUL_CHARS = 0
HUMAN_DELAY = 3
MAX_COMMENTS = 200  # Process up to 200 comments


# =========================
# DB SETUP
# =========================

engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)


# =========================
# HELPERS
# =========================

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


def is_post_deleted_or_removed(page: Page) -> bool:
    """Check if post content is deleted/removed"""
    try:
        text = page.content().lower()
        return any(marker in text for marker in [
            "[deleted]", "[removed]", "this post was deleted", 
            "this post has been removed", "sorry, this post has been removed"
        ])
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

def process_one_reddit(session, page: Page):
    """Process a single Reddit URL"""
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
                SavedItem.raw_url.contains("reddit.com")
                | SavedItem.raw_url.contains("redd.it")
            )
            .order_by(SavedItem.id)
            .limit(1)
        )
        .scalar_one_or_none()
    )

    if not item:
        return None

    original_url = canonicalize_url(item.raw_url)
    item.canonical_url = original_url

    print(f"\n{'='*60}")
    print(f"Processing: {original_url}")
    print(f"{'='*60}")

    # =====================
    # 1️⃣ TRY LIVE REDDIT
    # =====================
    
    try:
        print("🌐 Attempting live Reddit...")
        page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
        
        # Wait for page to load completely
        wait_for_page_loaded(page)
        # ADD these lines:
        accept_nsfw_if_present(page)  # First attempt
        page.wait_for_timeout(1000)
        accept_nsfw_if_present(page)  # Second attempt (in case of delayed modal)
        
        # Handle NSFW prompt EARLY
        accept_nsfw_if_present(page)
        
        # Kill videos IMMEDIATELY and repeatedly
        kill_all_videos(page)
        page.wait_for_timeout(1000)
        kill_all_videos(page)  # Kill again after any dynamic loads
        
        # Scroll to trigger lazy-loaded comments
        scroll_to_load_comments(page)
        
        # Kill videos one more time
        kill_all_videos(page)
        
        # Human-like delay
        time.sleep(HUMAN_DELAY)
        
        # Check post type
        is_media = is_video_or_image_post(page)
        is_deleted = is_post_deleted_or_removed(page)
        
        print(f"  📊 Media post: {is_media}")
        print(f"  📊 Deleted/Removed: {is_deleted}")
        
        # ===== EXTRACT EVERYTHING =====
        
        # 1. Title (ALWAYS)
        title = extract_post_title(page)
        
        # 2. Post body (ALWAYS try, even for videos)
        post_text = extract_post_text(page)
        
        # 3. Comments (ALWAYS - this is KEY)
        print("  🔍 Extracting comments...")
        comments_text = extract_all_comments(page)
        
        # ===== COMBINE =====
        parts = []
        
        if title:
            parts.append(f"TITLE:\n{title}")
        
        if post_text:
            parts.append(f"\nPOST:\n{post_text}")
        
        if comments_text:
            parts.append(f"\n\nCOMMENTS:\n{comments_text}")
        
        combined = "\n\n".join(parts)
        
        print(f"\n  📏 Total extracted: {len(combined)} chars")
        print(f"     - Title: {len(title)} chars")
        print(f"     - Post: {len(post_text)} chars")
        print(f"     - Comments: {len(comments_text)} chars")
        
        # ✅ SUCCESS if we have meaningful content
        # Accept if we have ANYTHING - title, post body, or comments
        if title or post_text or comments_text:
            item.title = title or title_from_url(original_url)
            item.extracted_text = combined
            item.status = "crawled"
            session.commit()
            print(f"\n✅ SUCCESS - Saved {len(combined)} chars")
            return f"✅ OK reddit crawled (live): {original_url}"
        
        print("\n  ⚠️  Not enough content from live site, trying Wayback...")
        
    except Exception as e:
        print(f"\n  ❌ Live Reddit failed: {e}")
        print("  🔄 Trying Wayback Machine...")

    # =====================
    # 2️⃣ TRY WAYBACK MACHINE
    # =====================
    
    try:
        wb_url = force_wayback(original_url)
        print(f"\n🕰️  Wayback URL: {wb_url}")
        
        page.goto(wb_url, wait_until="domcontentloaded", timeout=60000)
        
        # Wait and kill videos
        wait_for_page_loaded(page, timeout=45000)
        kill_all_videos(page)
        scroll_to_load_comments(page)
        kill_all_videos(page)
        
        time.sleep(HUMAN_DELAY)
        
        # Extract all content
        wb_title = extract_post_title(page)
        wb_post = extract_post_text(page)
        wb_comments = extract_all_comments(page)
        
        wb_parts = []
        if wb_title:
            wb_parts.append(f"TITLE:\n{wb_title}")
        if wb_post:
            wb_parts.append(f"\nPOST:\n{wb_post}")
        if wb_comments:
            wb_parts.append(f"\n\nCOMMENTS:\n{wb_comments}")
        
        wb_combined = "\n\n".join(wb_parts)
        
        print(f"  📏 Wayback extracted: {len(wb_combined)} chars")
        
        if wb_title or wb_post or wb_comments:
            item.title = wb_title or title_from_url(original_url)
            item.extracted_text = wb_combined
            item.status = "crawled_wayback"
            session.commit()
            print(f"\n✅ SUCCESS via Wayback - Saved {len(wb_combined)} chars")
            return f"✅ OK reddit crawled (wayback): {original_url}"
        
        print("\n  ⚠️  Wayback also insufficient...")
        
    except Exception as e:
        print(f"\n  ❌ Wayback failed: {e}")

    # =====================
    # 3️⃣ FINAL FALLBACK
    # =====================
    
    print("\n  ⚠️  Falling back to title-only")
    item.title = title if 'title' in locals() else title_from_url(original_url)
    item.extracted_text = None
    item.status = "title_only"
    item.summary = "Reddit post unavailable (deleted/removed/extraction failed)"
    session.commit()
    return f"⚠️  TITLE_ONLY reddit: {original_url}"


# =========================
# MAIN LOOP
# =========================

def main():
    session = SessionLocal()

    with sync_playwright() as p:
        # Try persistent profile first, fallback to regular launch
        try:
            print("🚀 Launching browser with profile...")
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=document-user-activation-required",
                    "--mute-audio",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            print("✓ Browser launched with profile")
            
        except Exception as e:
            print(f"⚠️  Profile launch failed: {e}")
            print("🔄 Launching without persistent profile...")
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=document-user-activation-required",
                    "--mute-audio",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            print("✓ Browser launched")

        page = context.new_page()
        page.set_default_navigation_timeout(60000)
        page.set_default_timeout(30000)

        n = 0
        while True:
            try:
                msg = process_one_reddit(session, page)
                if msg is None:
                    print("\n✓ No more Reddit URLs to process")
                    break
                print(f"\n{msg}")
                n += 1
                time.sleep(HUMAN_DELAY)
                
            except KeyboardInterrupt:
                print("\n\n⚠️  Interrupted by user")
                break
                
            except Exception as e:
                print(f"\n❌ Error processing item: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5)
                continue

        context.close()

    session.close()
    print(f"\n{'='*60}")
    print(f"✅ DONE reddit crawl: processed={n} items")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()