import time
import re
from pathlib import Path
from urllib.parse import urlsplit, urljoin
from sqlalchemy import select, or_, create_engine, text
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright, Page, Locator
from app.models import SavedItem
from app.ingest.canonicalize import canonicalize_url
from app.ingest.title_from_url import title_from_url
from migration.config import PRIMARY
from migration.db_factory import new_session
import argparse
import os
from dotenv import load_dotenv, dotenv_values
from contextlib import contextmanager
from app.browser_profile import launch_social_context

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"

HUMAN_DELAY = 3
MAX_SCROLL_PASSES = 14
MAX_REPLIES_CAPTURE = 30
MAX_THREAD_NEIGHBORS = 15
MIN_MEANINGFUL_CHARS = 25


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


PROFILE_DIR = Path(r"D:\playwright-x-profile")


# -------------------- generic helpers --------------------

def safe_inner_text(loc: Locator, timeout: int = 2500) -> str:
    try:
        return (loc.inner_text(timeout=timeout) or "").strip()
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
    text = re.sub(r"\bx\.com/\S+\b", "", text, flags=re.I)
    text = re.sub(r"\btwitter\.com/\S+\b", "", text, flags=re.I)
    return text


def parse_compact_number(value: str) -> float:
    s = (value or "").strip().replace(",", "").upper()
    if not s:
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])?", s)
    if not m:
        digits = re.sub(r"[^\d.]", "", s)
        if not digits:
            return 0.0
        try:
            return float(digits)
        except Exception:
            return 0.0
    num = float(m.group(1))
    suffix = m.group(2)
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return num * mult


def strip_x_noise(text: str) -> str:
    if not text:
        return ""

    text = strip_urls(text)
    lines = []
    skip_exact = {
        "follow",
        "following",
        "reply",
        "repost",
        "like",
        "bookmark",
        "share",
        "copy link",
        "show more",
        "show this thread",
        "view post engagements",
        "post your reply",
        "translate post",
        "show replies",
        "view community note",
        "this post may contain sensitive content",
        "read more",
        "sign up",
        "log in",
    }

    for raw in text.splitlines():
        line = collapse_ws(raw)
        if not line:
            continue

        lowered = line.lower()

        if lowered in skip_exact:
            continue
        if lowered.startswith("replying to "):
            continue
        if lowered.startswith("@") and len(lowered.split()) == 1:
            continue
        if re.fullmatch(r"\d+[smhdwy]", lowered):
            continue
        if re.fullmatch(r"[\W_]+", lowered):
            continue
        if re.fullmatch(r"\d+(\.\d+)?[kmb]?", lowered):
            continue
        if lowered in {"ad", "promoted"}:
            continue

        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def is_meaningful(text: str, minimum: int = MIN_MEANINGFUL_CHARS) -> bool:
    cleaned = strip_x_noise(text)
    return bool(cleaned and len(cleaned) >= minimum)


def first_meaningful_line(text: str) -> str:
    cleaned = strip_x_noise(text)
    for line in cleaned.splitlines():
        line = line.strip(" -–—|")
        if len(line) >= 8:
            return line[:120]
    return ""


def body_text(page: Page) -> str:
    try:
        return strip_x_noise(page.locator("body").inner_text(timeout=3000))
    except Exception:
        return ""


def current_page_title(page: Page) -> str:
    try:
        return (page.title() or "").strip()
    except Exception:
        return ""


def meta_content(page: Page, selector: str) -> str:
    try:
        return (page.locator(selector).first.get_attribute("content", timeout=1200) or "").strip()
    except Exception:
        return ""


def extract_meta_title(page: Page) -> str:
    for sel in [
        "meta[property='og:title']",
        "meta[name='twitter:title']",
        "meta[name='title']",
    ]:
        val = meta_content(page, sel)
        if val:
            return re.sub(r"\s*/\s*X\s*$", "", val).strip()
    return current_page_title(page)


def parse_status_id_from_url(url: str) -> str:
    m = re.search(r"/status(?:es)?/(\d+)", url or "")
    return m.group(1) if m else ""


def parse_handle_from_status_url(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)/status(?:es)?/\d+", url or "", re.I)
    return (m.group(1) or "").lower() if m else ""


def wait_for_page_loaded(page: Page, timeout: int = 45000):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(2200)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def accept_cookie_prompts(page: Page):
    selectors = [
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "[role='button']:has-text('Accept all cookies')",
        "[role='button']:has-text('Accept all')",
    ]
    for _ in range(3):
        clicked = False
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1500, force=True)
                    page.wait_for_timeout(900)
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


def is_logged_out_or_authwall(page: Page) -> tuple[bool, str]:
    url = page.url.lower()
    text = body_text(page).lower()

    if any(marker in url for marker in ["/i/flow/login", "/i/flow/signup", "/login", "/signup"]):
        return True, "authwall_url"

    markers = [
        "sign in to x",
        "join x today",
        "sign up for x",
        "log in to x",
        "create account",
    ]
    if sum(marker in text for marker in markers) >= 2:
        return True, "authwall_text"

    return False, ""


def is_not_visible_or_unavailable(page: Page, response_status: int | None) -> tuple[bool, str]:
    text = body_text(page).lower()

    if response_status is not None and response_status >= 400:
        return True, f"http_{response_status}"

    markers = [
        "this post is unavailable",
        "this post was deleted",
        "this account doesn’t exist",
        "this account doesn't exist",
        "these posts are protected",
        "are you sure you want to view these posts",
        "you’re unable to view these posts",
        "you're unable to view these posts",
        "this post violated the x rules",
        "something went wrong. try reloading",
        "page doesn’t exist",
        "page doesn't exist",
    ]
    for marker in markers:
        if marker in text:
            return True, marker.replace(" ", "_")
    return False, ""


def ensure_logged_in_x_session(page: Page):
    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
    wait_for_page_loaded(page)
    accept_cookie_prompts(page)

    logged_out, reason = is_logged_out_or_authwall(page)
    if logged_out:
        raise RuntimeError(f"X session is not already logged in: {reason}")


def click_all_matching(page: Page, selectors: list[str], rounds: int = 4):
    for _ in range(rounds):
        clicked_any = False
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 15)
                for i in range(count):
                    try:
                        node = loc.nth(i)
                        if node.is_visible():
                            node.click(timeout=1200, force=True)
                            page.wait_for_timeout(350)
                            clicked_any = True
                    except Exception:
                        continue
            except Exception:
                continue
        if not clicked_any:
            break


def expand_all_show_more(page: Page):
    selectors = [
        "[role='button']:has-text('Show more')",
        "button:has-text('Show more')",
        "[role='button']:has-text('Show this thread')",
        "button:has-text('Show this thread')",
        "[role='button']:has-text('View')",
        "[role='button']:has-text('More replies')",
        "[role='button']:has-text('Show replies')",
        "[role='button']:has-text('View more replies')",
    ]
    click_all_matching(page, selectors, rounds=6)


def scroll_detail_page(page: Page):
    stable = 0
    last_height = 0

    for _ in range(MAX_SCROLL_PASSES):
        kill_all_videos(page)
        expand_all_show_more(page)
        try:
            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
        except Exception:
            pass
        page.wait_for_timeout(900)

        try:
            h = int(page.evaluate("() => document.body.scrollHeight"))
        except Exception:
            h = last_height

        if h == last_height:
            stable += 1
        else:
            stable = 0
        last_height = h

        if stable >= 3:
            break

    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(600)
    expand_all_show_more(page)


# -------------------- article detection --------------------

def article_status_url(article: Locator) -> str:
    try:
        links = article.locator("a[href*='/status/'], a[href*='/statuses/']")
        count = min(links.count(), 10)
    except Exception:
        return ""

    for i in range(count):
        try:
            href = links.nth(i).get_attribute("href", timeout=1000)
        except Exception:
            href = None
        if not href:
            continue
        abs_url = urljoin("https://x.com", href)
        if "/status/" in abs_url or "/statuses/" in abs_url:
            return canonicalize_url(abs_url)
    return ""


def article_status_id(article: Locator) -> str:
    return parse_status_id_from_url(article_status_url(article))


def article_author_handle(article: Locator) -> str:
    url = article_status_url(article)
    handle = parse_handle_from_status_url(url)
    if handle:
        return handle

    try:
        links = article.locator("a[href^='/']")
        count = min(links.count(), 20)
    except Exception:
        return ""

    for i in range(count):
        try:
            href = links.nth(i).get_attribute("href", timeout=800) or ""
        except Exception:
            continue
        if re.fullmatch(r"/[A-Za-z0-9_]+", href):
            return href.strip("/").lower()
    return ""


def article_main_text(article: Locator) -> str:
    selectors = [
        "[data-testid='tweetText']",
        "div[lang]",
        "div[dir='auto']",
        "span",
    ]
    best = ""

    for sel in selectors:
        try:
            locs = article.locator(sel)
            count = min(locs.count(), 30)
            for i in range(count):
                text = safe_inner_text(locs.nth(i), timeout=1500)
                text = strip_x_noise(text)
                if len(text) > len(best):
                    best = text
        except Exception:
            continue

    return best.strip()


def article_quote_text(article: Locator, main_text: str) -> str:
    candidates = []

    selectors = [
        "div[data-testid='card.wrapper']",
        "div[data-testid='tweetPhoto']",
        "div[aria-labelledby][role='link']",
    ]
    for sel in selectors:
        try:
            locs = article.locator(sel)
            count = min(locs.count(), 8)
            for i in range(count):
                txt = strip_x_noise(safe_inner_text(locs.nth(i), timeout=1200))
                if txt and txt != main_text and len(txt) >= 20:
                    candidates.append(txt)
        except Exception:
            continue

    if not candidates:
        return ""

    candidates.sort(key=len, reverse=True)
    return candidates[0]


def article_community_note(article: Locator) -> str:
    selectors = [
        "[data-testid='birdwatch-pivot']",
        "[aria-label*='Community Notes' i]",
        "div:has-text('Readers added context')",
        "div:has-text('Community Notes')",
    ]
    best = ""
    for sel in selectors:
        try:
            locs = article.locator(sel)
            count = min(locs.count(), 5)
            for i in range(count):
                txt = strip_x_noise(safe_inner_text(locs.nth(i), timeout=1200))
                if len(txt) > len(best):
                    best = txt
        except Exception:
            continue
    return best


def detect_media_note(article: Locator) -> str:
    try:
        if article.locator("video, audio").count() > 0:
            return "ignored_video_media"
        if article.locator("[data-testid='tweetPhoto'], img").count() > 0:
            return "ignored_image_media"
        if article.locator("[data-testid='card.wrapper']").count() > 0:
            return "ignored_card_or_external_media"
        if article.locator("iframe").count() > 0:
            return "ignored_embedded_media"
    except Exception:
        pass
    return ""


def is_x_ad_or_noise(text: str) -> bool:
    lowered = (text or "").lower()
    markers = [
        "promoted",
        "\nad\n",
        "who to follow",
        "relevant people",
        "trends for you",
        "subscribe to premium",
        "discover more",
        "you might like",
        "show more posts",
        "sign up to get the full experience",
        "new to x?",
        "don’t miss what’s happening",
        "don't miss what's happening",
    ]
    wrapped = f"\n{lowered}\n"
    return any(m in wrapped or m in lowered for m in markers)


def article_quality_score(article: Locator, target_status_id: str) -> int:
    score = 0
    txt = strip_x_noise(safe_inner_text(article, timeout=1500))
    if not txt:
        return -999
    score += min(len(txt), 400)
    if article_status_id(article) == target_status_id:
        score += 1000
    if article_main_text(article):
        score += 120
    if "replying to" in txt.lower():
        score += 20
    if not is_x_ad_or_noise(txt):
        score += 40
    return score


def collect_visible_articles(page: Page) -> list[Locator]:
    try:
        locs = page.locator("article")
        count = min(locs.count(), 120)
    except Exception:
        return []
    return [locs.nth(i) for i in range(count)]


def select_target_article(page: Page, target_status_id: str) -> tuple[Locator | None, int]:
    articles = collect_visible_articles(page)
    if not articles:
        return None, -1

    best_idx = -1
    best_score = -99999

    for i, article in enumerate(articles):
        try:
            score = article_quality_score(article, target_status_id)
        except Exception:
            continue
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx == -1:
        return None, -1

    return articles[best_idx], best_idx


# -------------------- engagement + replies --------------------

def parse_engagement_from_text(text: str, label_words: list[str]) -> float:
    patterns = [
        rf"(\d+(?:\.\d+)?[KMB]?)\s+(?:{'|'.join(label_words)})\b",
        rf"\b(?:{'|'.join(label_words)})\s+(\d+(?:\.\d+)?[KMB]?)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return parse_compact_number(m.group(1))
    return 0.0


def extract_likes_robust(article: Locator) -> float:
    candidates = []

    try:
        groups = article.locator("[role='group']")
        count = min(groups.count(), 8)
        for i in range(count):
            try:
                aria = groups.nth(i).get_attribute("aria-label", timeout=1000) or ""
            except Exception:
                aria = ""
            if aria:
                candidates.append(aria)
    except Exception:
        pass

    try:
        buttons = article.locator("[data-testid='like'], [data-testid='unlike'], [aria-label*='like' i]")
        count = min(buttons.count(), 8)
        for i in range(count):
            try:
                aria = buttons.nth(i).get_attribute("aria-label", timeout=800) or ""
            except Exception:
                aria = ""
            txt = safe_inner_text(buttons.nth(i), timeout=800)
            if aria:
                candidates.append(aria)
            if txt:
                candidates.append(txt)
    except Exception:
        pass

    try:
        candidates.append(safe_inner_text(article, timeout=1200))
    except Exception:
        pass

    best = 0.0
    for c in candidates:
        value = parse_engagement_from_text(c, ["like", "likes"])
        if value > best:
            best = value
    return best


def extract_replies_robust(article: Locator) -> float:
    candidates = []
    try:
        groups = article.locator("[role='group']")
        count = min(groups.count(), 8)
        for i in range(count):
            try:
                aria = groups.nth(i).get_attribute("aria-label", timeout=1000) or ""
            except Exception:
                aria = ""
            if aria:
                candidates.append(aria)
    except Exception:
        pass

    try:
        candidates.append(safe_inner_text(article, timeout=1200))
    except Exception:
        pass

    best = 0.0
    for c in candidates:
        value = parse_engagement_from_text(c, ["reply", "replies"])
        if value > best:
            best = value
    return best


def reply_quality_score(article: Locator, main_like_score: float) -> tuple[float, float, float]:
    text = article_main_text(article)
    likes = extract_likes_robust(article)
    replies = extract_replies_robust(article)

    base = len(strip_x_noise(text))
    punctuation_bonus = 12 if re.search(r"[.!?]", text) else 0
    sentence_bonus = min(text.count(".") + text.count("!") + text.count("?"), 6) * 3
    engagement_bonus = min(likes / 10.0, 500) + min(replies * 3.0, 300)

    quality = base + punctuation_bonus + sentence_bonus + engagement_bonus
    return likes, replies, quality


def dedupe_preserve(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        key = collapse_ws(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# -------------------- extraction plan --------------------

def extract_thread_context(
    articles: list[Locator],
    target_idx: int,
    target_article: Locator,
) -> tuple[str, str, list[Locator]]:
    target_handle = article_author_handle(target_article)
    target_status = article_status_id(target_article)

    thread_above = []
    thread_below = []
    reply_candidates: list[Locator] = []

    for idx, article in enumerate(articles):
        if idx == target_idx:
            continue

        raw_text = strip_x_noise(safe_inner_text(article, timeout=1200))
        if not is_meaningful(raw_text, minimum=12):
            continue
        if is_x_ad_or_noise(raw_text):
            continue

        status_id = article_status_id(article)
        handle = article_author_handle(article)

        if status_id and status_id == target_status:
            continue

        distance = abs(idx - target_idx)
        same_author = bool(target_handle and handle and handle == target_handle)

        if same_author and distance <= MAX_THREAD_NEIGHBORS:
            text = article_main_text(article) or raw_text
            text = strip_x_noise(text)
            if is_meaningful(text, minimum=20):
                if idx < target_idx:
                    thread_above.append(text)
                else:
                    thread_below.append(text)
            continue

        if idx > target_idx:
            reply_candidates.append(article)

    thread_above = dedupe_preserve(thread_above[-MAX_THREAD_NEIGHBORS:])
    thread_below = dedupe_preserve(thread_below[:MAX_THREAD_NEIGHBORS])

    return (
        "\n\n---THREAD_POST---\n\n".join(thread_above),
        "\n\n---THREAD_POST---\n\n".join(thread_below),
        reply_candidates,
    )


def classify_replies(reply_candidates: list[Locator], main_like_score: float) -> tuple[str, str, str]:
    less_than = None
    greater_or_equal = None
    comments_all = []

    fallback_main_score = main_like_score if main_like_score > 0 else 1.0

    for article in reply_candidates[:MAX_REPLIES_CAPTURE]:
        raw = strip_x_noise(safe_inner_text(article, timeout=1500))
        if not is_meaningful(raw, minimum=15):
            continue
        if is_x_ad_or_noise(raw):
            continue

        text = article_main_text(article) or raw
        text = strip_x_noise(text)
        if not is_meaningful(text, minimum=15):
            continue

        likes, replies, quality = reply_quality_score(article, fallback_main_score)

        comments_all.append(text)

        payload = {
            "text": text,
            "likes": likes,
            "replies": replies,
            "quality": quality,
        }

        if likes < fallback_main_score:
            if less_than is None or (payload["quality"], payload["likes"], payload["replies"]) > (
                less_than["quality"], less_than["likes"], less_than["replies"]
            ):
                less_than = payload
        else:
            if greater_or_equal is None or (payload["quality"], payload["likes"], payload["replies"]) > (
                greater_or_equal["quality"], greater_or_equal["likes"], greater_or_equal["replies"]
            ):
                greater_or_equal = payload

    comments_all = dedupe_preserve(comments_all)[:25]
    all_block = "\n\n---COMMENT---\n\n".join(comments_all)

    lt_block = less_than["text"] if less_than else ""
    gte_block = greater_or_equal["text"] if greater_or_equal else ""

    return lt_block, gte_block, all_block


def build_structured_text(
    *,
    visibility_note: str,
    media_note: str,
    title: str,
    thread_above: str,
    post_text: str,
    quote_text: str,
    community_note: str,
    thread_below: str,
    replies_lt_post: str,
    replies_gte_post: str,
    comments_all: str,
) -> str:
    parts = []

    if visibility_note:
        parts.append(f"VISIBILITY_NOTE:{visibility_note}")
    if media_note:
        parts.append(f"MEDIA_NOTE:{media_note}")
    if title:
        parts.append(f"TITLE:{title}")
    if thread_above:
        parts.append(f"THREAD_ABOVE:{thread_above}")
    if post_text:
        parts.append(f"POST:{post_text}")
    if quote_text:
        parts.append(f"QUOTE:{quote_text}")
    if community_note:
        parts.append(f"COMMUNITY_NOTE:{community_note}")
    if thread_below:
        parts.append(f"THREAD_BELOW:{thread_below}")
    if replies_lt_post:
        parts.append(f"TOP_REPLY_LT_POST:{replies_lt_post}")
    if replies_gte_post:
        parts.append(f"TOP_REPLY_GTE_POST:{replies_gte_post}")
    if comments_all:
        parts.append(f"COMMENTS:{comments_all}")

    return "\n\n".join(parts).strip()


def extract_body_meta_fallback(page: Page, current_url: str) -> tuple[str, str]:
    meta_title = extract_meta_title(page)
    txt = body_text(page)
    return (meta_title or title_from_url(current_url), txt or meta_title or title_from_url(current_url))


# -------------------- core crawl --------------------

def process_one_x(session, page: Page, mirror_session=None) -> str | None:
    session.rollback()

    item = (
        session.execute(
            select(SavedItem)
            .where(
                SavedItem.status.in_(["pending_crawl", "crawl_failed", "extraction_failed", "title_only"]),
                or_(
                    SavedItem.raw_url.contains("x.com"),
                    SavedItem.raw_url.contains("twitter.com"),
                ),
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

    try:
        print("=" * 70)
        print(f"Processing X: {original_url}")
        print("=" * 70)

        response = page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
        response_status = response.status if response else None

        wait_for_page_loaded(page)
        accept_cookie_prompts(page)
        kill_all_videos(page)
        expand_all_show_more(page)

        logged_out, auth_reason = is_logged_out_or_authwall(page)
        if logged_out:
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=title_from_url(original_url),
                extracted_text=None,
                status="title_only",
                summary=f"X authwall/login state detected: {auth_reason}",
                mirror_session=mirror_session,
            )
            return f"TITLE_ONLY X authwall: {original_url}"

        not_visible, visibility_reason = is_not_visible_or_unavailable(page, response_status)
        if not_visible:
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=title_from_url(original_url),
                extracted_text=None,
                status="title_only",
                summary=f"X post not visible to current logged-in user: {visibility_reason}",
                mirror_session=mirror_session,
            )
            return f"TITLE_ONLY X not_visible: {original_url}"

        scroll_detail_page(page)
        kill_all_videos(page)
        time.sleep(HUMAN_DELAY)

        target_status_id = parse_status_id_from_url(original_url)
        articles = collect_visible_articles(page)
        target_article, target_idx = select_target_article(page, target_status_id)

        if target_article is None or target_idx < 0:
            raise RuntimeError("No reliable target article found on X detail page")

        post_text = article_main_text(target_article)
        quote_text = article_quote_text(target_article, post_text)
        community_note = article_community_note(target_article)
        media_note = detect_media_note(target_article)
        main_like_score = extract_likes_robust(target_article)

        thread_above, thread_below, reply_candidates = extract_thread_context(
            articles,
            target_idx,
            target_article,
        )

        reply_lt_post, reply_gte_post, comments_all = classify_replies(reply_candidates, main_like_score)

        title = (
            first_meaningful_line(post_text)
            or first_meaningful_line(thread_above)
            or first_meaningful_line(thread_below)
            or extract_meta_title(page)
            or title_from_url(original_url)
            or "X Post"
        )

        combined = build_structured_text(
            visibility_note="visible_to_current_logged_in_user",
            media_note=media_note,
            title=title,
            thread_above=thread_above,
            post_text=post_text,
            quote_text=quote_text,
            community_note=community_note,
            thread_below=thread_below,
            replies_lt_post=reply_lt_post,
            replies_gte_post=reply_gte_post,
            comments_all=comments_all,
        )

        if any([
            is_meaningful(post_text, minimum=15),
            is_meaningful(thread_above, minimum=15),
            is_meaningful(thread_below, minimum=15),
            is_meaningful(comments_all, minimum=15),
            is_meaningful(quote_text, minimum=15),
        ]):
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
            return f"OK X crawled: {original_url}"

        fb_title, fb_text = extract_body_meta_fallback(page, original_url)
        fallback_structured = build_structured_text(
            visibility_note="visible_to_current_logged_in_user_fallback_body_meta",
            media_note=media_note,
            title=fb_title,
            thread_above="",
            post_text=fb_text,
            quote_text="",
            community_note="",
            thread_below="",
            replies_lt_post="",
            replies_gte_post="",
            comments_all="",
        )

        if is_meaningful(fb_text, minimum=20):
            save_final_result(
                session,
                item_id,
                canonical_url=original_url,
                title=fb_title,
                extracted_text=fallback_structured,
                status="crawled",
                summary="X body/meta fallback used",
                mirror_session=mirror_session,
            )
            return f"OK X crawled (body/meta fallback): {original_url}"

        save_final_result(
            session,
            item_id,
            canonical_url=original_url,
            title=fb_title or title_from_url(original_url),
            extracted_text=None,
            status="title_only",
            summary="X post visible but no meaningful text/thread/comments could be extracted",
            mirror_session=mirror_session,
        )
        return f"TITLE_ONLY X no_meaningful_text: {original_url}"

    except KeyboardInterrupt:
        session.rollback()
        raise

    except Exception as e:
        session.rollback()
        item_live = session.get(SavedItem, item_id)
        if item_live is not None:
            item_live.canonical_url = original_url
            item_live.status = "crawl_failed"
            item_live.summary = f"X crawl failed: {e}"
            commit_and_mirror(session, item_id, mirror_session)
        return f"ERROR X crawl_failed: {original_url} ({e})"


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
    item.summary = summary
    item.status = status

    session.commit()
    session.expire_all()

    if mirror_session is not None:
        try:
            mirror_saved_item_by_id(session, mirror_session, item_id)
            mirror_session.commit()
        except Exception as e:
            mirror_session.rollback()
            print(f"⚠ mirror sync failed; local sqlite write kept: {e}")


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

        ensure_logged_in_x_session(page)

        n = 0
        while limit is None or n < limit:
            msg = process_one_x(session, page, mirror_session=mirror_session)
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