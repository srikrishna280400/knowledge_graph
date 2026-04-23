import time
import re
from pathlib import Path
from urllib.parse import urlsplit, urljoin
from sqlalchemy import select, create_engine, text
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
from playwright.sync_api import sync_playwright, Page, Locator
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url
from migration.config import PRIMARY
from migration.db_factory import new_session
import argparse
import os
from dotenv import load_dotenv, dotenv_values
from app.browser_profile import launch_social_context


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"

HUMAN_DELAY = 3
MAX_COMMENTS = 20
MIN_MEANINGFUL_CHARS = 40

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

PROFILE_DIR = Path(r"D:\playwright-linkedin-profile")

# -------------------- helpers --------------------

def safe_inner_text(loc: Locator, timeout: int = 3000) -> str:
    try:
        return loc.inner_text(timeout=timeout).strip()
    except Exception:
        return ""

def collapse_ws(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def strip_urls(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text, flags=re.I)
    text = re.sub(r"\bwww\.\S+\b", "", text, flags=re.I)
    text = re.sub(r"\blinkedin\.com/\S+\b", "", text, flags=re.I)
    return text

def strip_linkedin_noise(text: str) -> str:
    if not text:
        return ""

    text = strip_urls(text)
    lines: list[str] = []

    ui_exact = {
        "like",
        "comment",
        "repost",
        "send",
        "copy link",
        "follow",
        "following",
        "connect",
        "message",
        "translate",
        "see translation",
        "edited",
        "more",
        "show more",
        "show less",
        "view more comments",
        "load more comments",
        "load previous replies",
        "view previous comments",
        "view more replies",
        "view previous replies",
        "most relevant",
        "top",
        "recent",
        "all comments",
        "add a comment",
        "sign in",
        "join now",
    }

    for raw in text.splitlines():
        line = collapse_ws(raw)
        if not line:
            continue

        lowered = line.lower().strip()

        if lowered in ui_exact:
            continue

        if re.fullmatch(r"\d+\s+(reactions?|comments?|reposts?)", lowered):
            continue

        if re.fullmatch(r"\d+[smhdwy]\b", lowered):
            continue

        if re.fullmatch(r"[·•]", lowered):
            continue

        if re.fullmatch(r"[\W_]+", lowered):
            continue

        if re.fullmatch(r"#\w+", line):
            continue

        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

def is_meaningful(text: str, minimum: int = MIN_MEANINGFUL_CHARS) -> bool:
    return bool(text and len(strip_linkedin_noise(text)) >= minimum)

def first_meaningful_line(text: str) -> str:
    cleaned = strip_linkedin_noise(text)
    for line in cleaned.splitlines():
        line = line.strip(" -–—|")
        if len(line) >= 8:
            return line[:120]
    return ""

def linkedin_page_title(page: Page) -> str:
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", title).strip()
    return title

def meta_content(page: Page, selector: str) -> str:
    try:
        value = page.locator(selector).first.get_attribute("content", timeout=1500)
        return (value or "").strip()
    except Exception:
        return ""

def extract_meta_title(page: Page) -> str:
    for sel in [
        "meta[property='og:title']",
        "meta[name='title']",
        "meta[name='twitter:title']",
    ]:
        val = meta_content(page, sel)
        if val:
            val = re.sub(r"\s*\|\s*LinkedIn.*$", "", val).strip()
            return val
    return linkedin_page_title(page)

def wait_for_page_loaded(page: Page, timeout: int = 45000):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(2500)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

def accept_cookie_prompts(page: Page):
    selectors = [
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button[action-type='ACCEPT']",
        "[aria-label*='Accept' i]",
        "[data-tracking-control-name*='accept' i]",
    ]
    for _ in range(3):
        clicked = False
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1500, force=True)
                    page.wait_for_timeout(1200)
                    clicked = True
            except Exception:
                continue
        if not clicked:
            break

def kill_all_videos(page: Page):
    try:
        page.evaluate(
            """
            () => {
              document.querySelectorAll("video, audio").forEach(v => {
                try {
                  v.pause();
                  v.muted = true;
                  v.currentTime = 0;
                  v.removeAttribute("src");
                  v.load();
                } catch(e) {}
              });
              if (window.HTMLMediaElement && window.HTMLMediaElement.prototype.play) {
                window.HTMLMediaElement.prototype.play = function() {
                  try { this.pause(); } catch(e) {}
                  return Promise.resolve();
                };
              }
            }
            """
        )
    except Exception:
        pass

def body_text(page: Page) -> str:
    try:
        return strip_linkedin_noise(page.locator("body").inner_text(timeout=3000))
    except Exception:
        return ""

def is_logged_out_or_authwall(page: Page) -> tuple[bool, str]:
    text = body_text(page).lower()
    current_url = page.url.lower()

    auth_markers = [
        "/authwall",
        "/uas/login",
        "/checkpoint/",
        "/signup",
    ]
    if any(marker in current_url for marker in auth_markers):
        return True, "authwall_url"

    text_markers = [
        "join now",
        "sign in",
        "new to linkedin",
        "sign in to see",
        "continue to linkedin",
    ]
    if sum(marker in text for marker in text_markers) >= 2:
        return True, "authwall_text"

    return False, ""

def is_not_visible_or_unavailable(page: Page, response_status: int | None) -> tuple[bool, str]:
    text = body_text(page).lower()

    if response_status is not None and response_status >= 400:
        return True, f"http_{response_status}"

    markers = [
        "this post is unavailable",
        "the post you’re looking for can't be displayed",
        "the post you're looking for can't be displayed",
        "content unavailable",
        "this content is not available",
        "page not found",
        "profile unavailable",
        "this page doesn’t exist",
        "this page doesn't exist",
        "member-only content",
        "not visible",
    ]
    for marker in markers:
        if marker in text:
            return True, marker.replace(" ", "_")

    return False, ""

def ensure_logged_in_linkedin_session(page: Page):
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    wait_for_page_loaded(page)
    accept_cookie_prompts(page)
    logged_out, reason = is_logged_out_or_authwall(page)
    if logged_out:
        raise RuntimeError(f"LinkedIn session is not already logged in: {reason}")

def click_all_matching(page: Page, selectors: list[str], rounds: int = 4):
    for _ in range(rounds):
        clicked_any = False
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 12)
                for i in range(count):
                    try:
                        node = loc.nth(i)
                        if node.is_visible():
                            node.click(timeout=1200, force=True)
                            page.wait_for_timeout(400)
                            clicked_any = True
                    except Exception:
                        continue
            except Exception:
                continue
        if not clicked_any:
            break

def expand_all_see_more(page: Page):
    selectors = [
        "button:has-text('see more')",
        "button:has-text('See more')",
        "span:has-text('see more')",
        "span:has-text('See more')",
        "button[aria-label*='more' i]",
        "[role='button']:has-text('more')",
    ]
    click_all_matching(page, selectors, rounds=5)

def expand_comment_threads(page: Page):
    selectors = [
        "button:has-text('Load more comments')",
        "button:has-text('View more comments')",
        "button:has-text('View previous comments')",
        "button:has-text('Load previous replies')",
        "button:has-text('View more replies')",
        "button:has-text('View previous replies')",
        "button:has-text('Show more comments')",
        "button:has-text('Show more replies')",
        "[role='button']:has-text('comments')",
        "[role='button']:has-text('replies')",
    ]
    click_all_matching(page, selectors, rounds=6)

def scroll_for_comments(page: Page):
    last_height = 0
    stable = 0

    for _ in range(12):
        kill_all_videos(page)
        expand_all_see_more(page)
        expand_comment_threads(page)

        try:
            height = page.evaluate("() => document.body.scrollHeight")
        except Exception:
            height = last_height

        try:
            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
        except Exception:
            pass

        page.wait_for_timeout(900)

        if height == last_height:
            stable += 1
        else:
            stable = 0
        last_height = height

        if stable >= 3:
            break

    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(800)
    expand_all_see_more(page)
    expand_comment_threads(page)

def candidate_text(loc: Locator) -> str:
    return strip_linkedin_noise(safe_inner_text(loc, timeout=2500))

def find_best_post_root(page: Page) -> Locator | None:
    selectors = [
        "main div.feed-shared-update-v2",
        "main [data-id^='urn:li:activity:']",
        "main [data-urn*='activity:']",
        "main article",
        "main div[data-view-name*='feed']",
        "main section",
    ]

    best_loc = None
    best_score = -1

    for sel in selectors:
        try:
            locs = page.locator(sel)
            count = min(locs.count(), 10)
            for i in range(count):
                loc = locs.nth(i)
                try:
                    if not loc.is_visible():
                        continue
                except Exception:
                    continue

                text = candidate_text(loc)
                score = len(text)

                if "comment" in text.lower():
                    score += 10
                if "repost" in text.lower() or "shared" in text.lower():
                    score += 5

                if score > best_score and score >= 80:
                    best_score = score
                    best_loc = loc
        except Exception:
            continue

    return best_loc

def extract_longest_from_scope(scope: Locator, selectors: list[str], minimum: int = 20) -> str:
    best = ""
    for sel in selectors:
        try:
            locs = scope.locator(sel)
            count = min(locs.count(), 12)
            for i in range(count):
                text = candidate_text(locs.nth(i))
                if len(text) > len(best):
                    best = text
        except Exception:
            continue
    if len(best) >= minimum:
        return best
    return ""

def detect_media_note(scope: Locator) -> str:
    try:
        if scope.locator("video, audio").count() > 0:
            return "ignored_video_media"
        if scope.locator("iframe").count() > 0:
            return "ignored_embedded_media"
        if scope.locator("img").count() > 0:
            return "ignored_image_media"
        if scope.locator("[aria-label*='document' i], [class*='document'], [data-test-id*='document']").count() > 0:
            return "ignored_document_media"
        if scope.locator("[aria-label*='pdf' i], [href*='.pdf']").count() > 0:
            return "ignored_pdf_media"
    except Exception:
        pass
    return ""

def resolve_original_shared_post_url(page: Page, current_url: str) -> str:
    root = find_best_post_root(page)
    if root is None:
        return current_url

    root_text = candidate_text(root).lower()
    if "reposted this" not in root_text and "shared this" not in root_text and "reshared" not in root_text:
        return current_url

    candidates = root.locator("a[href*='/feed/update/'], a[href*='/posts/']")
    try:
        count = min(candidates.count(), 20)
    except Exception:
        count = 0

    normalized_current = canonicalize_url(current_url)

    for i in range(count):
        try:
            href = candidates.nth(i).get_attribute("href", timeout=1200)
        except Exception:
            href = None
        if not href:
            continue

        absolute = urljoin("https://www.linkedin.com", href)
        if "linkedin.com" not in absolute:
            continue

        absolute = canonicalize_url(absolute)
        if absolute == normalized_current:
            continue

        if "/feed/update/" in absolute or "/posts/" in absolute:
            return absolute

    return current_url

def extract_post_text(root: Locator) -> str:
    selectors = [
        ".feed-shared-inline-show-more-text",
        ".feed-shared-update-v2__description",
        ".update-components-text",
        "[data-test-id*='update-components-text']",
        "span.break-words",
        "div[dir='ltr']",
        "p",
    ]
    text = extract_longest_from_scope(root, selectors, minimum=20)
    if text:
        return text

    fallback = candidate_text(root)
    return fallback if len(fallback) >= 20 else ""

def extract_quote_text(root: Locator, post_text: str) -> str:
    selectors = [
        ".feed-shared-article",
        ".feed-shared-external-video__meta",
        ".feed-shared-mini-update-v2",
        ".feed-shared-image__container",
        ".update-components-reshared-update",
        "[data-test-id*='reshare']",
    ]
    quote = extract_longest_from_scope(root, selectors, minimum=20)
    if quote and quote != post_text:
        return quote
    return ""

def best_comment_selector(page: Page) -> str | None:
    selectors = [
        "main li.comments-comment-item",
        "main .comments-comment-item",
        "main [data-test-id='comments-comment-item']",
        "main article.comments-comment-item",
        "main [class*='comments-comment-item']",
        "main [data-urn*='comment']",
    ]
    best_sel = None
    best_count = 0
    for sel in selectors:
        try:
            count = page.locator(sel).count()
            if count > best_count:
                best_count = count
                best_sel = sel
        except Exception:
            continue
    return best_sel

def clean_comment_block(text: str) -> str:
    text = strip_linkedin_noise(text)
    text = re.sub(r"\b\d+\s*(reactions?|likes?)\b", "", text, flags=re.I)
    text = re.sub(r"\b(reply|like|repost|send)\b", "", text, flags=re.I)
    return collapse_ws(text)

def extract_comments(page: Page) -> str:
    sel = best_comment_selector(page)
    if not sel:
        return ""

    out: list[str] = []
    seen: set[str] = set()

    try:
        locs = page.locator(sel)
        count = min(locs.count(), MAX_COMMENTS)
    except Exception:
        return ""

    for i in range(count):
        try:
            raw = locs.nth(i).inner_text(timeout=2500)
        except Exception:
            continue

        cleaned = clean_comment_block(raw)
        if not is_meaningful(cleaned, minimum=20):
            continue
        if cleaned in seen:
            continue

        seen.add(cleaned)
        out.append(cleaned)

    return "\n\n---COMMENT---\n\n".join(out)

def derive_title(page: Page, post_text: str, quote_text: str, comments_text: str, current_url: str) -> str:
    for candidate in [
        first_meaningful_line(post_text),
        first_meaningful_line(quote_text),
        first_meaningful_line(comments_text),
        extract_meta_title(page),
        title_from_url(current_url),
        "LinkedIn Post",
    ]:
        if candidate:
            return candidate[:120]
    return "LinkedIn Post"

def build_structured_text(
    *,
    visibility_note: str,
    media_note: str,
    title: str,
    post_text: str,
    quote_text: str,
    comments_text: str,
) -> str:
    parts: list[str] = []

    if visibility_note:
        parts.append(f"VISIBILITY_NOTE:{visibility_note}")
    if media_note:
        parts.append(f"MEDIA_NOTE:{media_note}")
    if title:
        parts.append(f"TITLE:{title}")
    if post_text:
        parts.append(f"POST:{post_text}")
    if quote_text:
        parts.append(f"QUOTE:{quote_text}")
    if comments_text:
        parts.append(f"COMMENTS:{comments_text}")

    return "\n\n".join(parts).strip()

def extract_body_meta_fallback(page: Page, current_url: str) -> tuple[str, str]:
    meta_title = extract_meta_title(page)
    text = body_text(page)

    noisy_markers = [
        "join now",
        "sign in",
        "new to linkedin",
        "agree & join linkedin",
    ]
    lines = []
    for line in text.splitlines():
        lowered = line.lower().strip()
        if any(marker == lowered for marker in noisy_markers):
            continue
        lines.append(line)

    cleaned = "\n".join(lines).strip()
    if not cleaned:
        cleaned = meta_title or title_from_url(current_url)

    return meta_title or title_from_url(current_url), cleaned

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

def process_one_linkedin(session, page: Page, mirror_session=None) -> str | None:
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
            .where(SavedItem.raw_url.contains("linkedin.com"))
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

    title = title_from_url(original_url)

    try:
        print("=" * 70)
        print(f"Processing LinkedIn: {original_url}")
        print("=" * 70)

        response = page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
        response_status = response.status if response else None

        wait_for_page_loaded(page)
        accept_cookie_prompts(page)
        kill_all_videos(page)
        expand_all_see_more(page)

        logged_out, auth_reason = is_logged_out_or_authwall(page)
        if logged_out:
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=title,
                extracted_text=None,
                status="title_only",
                summary=f"LinkedIn authwall/login state detected: {auth_reason}",
                mirror_session=mirror_session,
            )
            return f"TITLE_ONLY linkedin authwall: {original_url}"

        not_visible, visibility_reason = is_not_visible_or_unavailable(page, response_status)
        if not_visible:
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=title,
                extracted_text=None,
                status="title_only",
                summary=f"LinkedIn post not visible to current logged-in user: {visibility_reason}",
                mirror_session=mirror_session,
            )
            return f"TITLE_ONLY linkedin not_visible: {original_url}"

        resolved_url = resolve_original_shared_post_url(page, original_url)
        if resolved_url != original_url:
            print(f"↪ reshare detected; hopping to original post: {resolved_url}")
            original_url = resolved_url
            response = page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
            response_status = response.status if response else None
            wait_for_page_loaded(page)
            accept_cookie_prompts(page)
            kill_all_videos(page)
            expand_all_see_more(page)

            logged_out, auth_reason = is_logged_out_or_authwall(page)
            if logged_out:
                save_final_result(
                    session,
                    item_id,
                    canonical_url=original_url,
                    title=title_from_url(original_url),
                    extracted_text=None,
                    status="title_only",
                    summary=f"LinkedIn authwall/login state detected after reshare redirect: {auth_reason}",
                    mirror_session=mirror_session,
                )
                return f"TITLE_ONLY linkedin authwall_after_redirect: {original_url}"

            not_visible, visibility_reason = is_not_visible_or_unavailable(page, response_status)
            if not_visible:
                save_final_result(
                    session,
                    item_id,
                    canonical_url=original_url,
                    title=title_from_url(original_url),
                    extracted_text=None,
                    status="title_only",
                    summary=f"Original shared LinkedIn post not visible to current logged-in user: {visibility_reason}",
                    mirror_session=mirror_session,
                )
                return f"TITLE_ONLY linkedin original_not_visible: {original_url}"

        scroll_for_comments(page)
        kill_all_videos(page)
        time.sleep(HUMAN_DELAY)

        root = find_best_post_root(page)
        if root is None:
            raise RuntimeError("No reliable LinkedIn post root found")

        post_text = extract_post_text(root)
        quote_text = extract_quote_text(root, post_text)
        comments_text = extract_comments(page)
        media_note = detect_media_note(root)
        title = derive_title(page, post_text, quote_text, comments_text, original_url)

        combined = build_structured_text(
            visibility_note="visible_to_current_logged_in_user",
            media_note=media_note,
            title=title,
            post_text=post_text,
            quote_text=quote_text,
            comments_text=comments_text,
        )

        if is_meaningful(post_text) or is_meaningful(comments_text, minimum=20) or is_meaningful(quote_text):
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=title,
                extracted_text=combined,
                status="crawled",
                summary=None,
                mirror_session=mirror_session,
            )
            return f"OK linkedin crawled: {original_url}"

        fb_title, fb_text = extract_body_meta_fallback(page, original_url)
        fallback_combined = build_structured_text(
            visibility_note="visible_to_current_logged_in_user_fallback_body_meta",
            media_note=media_note,
            title=fb_title,
            post_text=fb_text,
            quote_text="",
            comments_text="",
        )

        if is_meaningful(fb_text, minimum=20):
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=fb_title,
                extracted_text=fallback_combined,
                status="crawled",
                summary="LinkedIn body/meta fallback used",
                mirror_session=mirror_session,
            )
            return f"OK linkedin crawled (body/meta fallback): {original_url}"

        save_final_result(
            session,
            item_id,
            canonical_url=original_url,
            title=fb_title or title_from_url(original_url),
            extracted_text=None,
            status="title_only",
            summary="LinkedIn post visible but no meaningful text/comments could be extracted",
            mirror_session=mirror_session,
        )
        return f"TITLE_ONLY linkedin no_meaningful_text: {original_url}"

    except KeyboardInterrupt:
        abandon_current_item(session)
        raise

    except Exception as e:
        abandon_current_item(session)
        item_live = session.get(SavedItem, item_id)
        if item_live is not None:
            item_live.canonical_url = original_url
            item_live.status = "crawl_failed"
            item_live.summary = f"LinkedIn crawl failed: {e}"
            commit_and_mirror(session, item_id, mirror_session)
        return f"ERROR linkedin crawl_failed: {original_url} ({e})"

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args()

def main():
    args = parse_args()
    limit = args.limit

    session = new_session(PRIMARY)
    with mirror_session_scope() as mirror_session, sync_playwright() as p:
        context = launch_social_context(p)
        page = context.new_page()

        page.set_default_navigation_timeout(60000)
        page.set_default_timeout(15000)

        ensure_logged_in_linkedin_session(page)

        n = 0
        while limit is None or n < limit:
            msg = process_one_linkedin(session, page, mirror_session=mirror_session)
            if msg is None:
                break
            print(msg)
            n += 1
            time.sleep(max(HUMAN_DELAY, 5))

        context.close()
        session.close()

        print(f"DONE processed={n} limit={limit}")

if __name__ == "__main__":
    main()