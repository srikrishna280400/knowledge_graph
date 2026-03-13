from __future__ import annotations

import re
from urllib.parse import urlsplit

REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com", "redd.it"}
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
POCKET_HOSTS = {"getpocket.com", "app.getpocket.com", "getpocket.app"}

# tweet: /<user>/status/<digits>  (works for x.com and twitter.com) [web:484]
X_STATUS_RE = re.compile(r"/[^/]+/status/\d+")

# reddit post: /r/<sub>/comments/<postid>/<slug>/...
# reddit comment: same + an additional trailing comment id segment. [web:479]
REDDIT_COMMENTS_RE = re.compile(r"^/r/[^/]+/comments/([a-z0-9]+)/", re.IGNORECASE)


def is_linkedin(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith("linkedin.com")


def is_x_tweet(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    return host in X_HOSTS and bool(X_STATUS_RE.search(parts.path))


def is_pocket(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in POCKET_HOSTS


def is_reddit(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in REDDIT_HOSTS or host.endswith("reddit.com")


def is_reddit_comment(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path or ""
    if host == "redd.it":
        return False  # shortlinks are posts
    if not is_reddit(url):
        return False
    m = REDDIT_COMMENTS_RE.match(path)
    if not m:
        return False
    # comment permalinks typically have an extra segment after the slug, e.g. .../<commentid>/ [web:479]
    segs = [s for s in path.strip("/").split("/") if s]
    # expected minimum for post: r, <sub>, comments, <postid>, <slug>
    # comment adds: <commentid>
    return len(segs) >= 6


def is_reddit_post(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path or ""
    if host == "redd.it":
        return True
    if not is_reddit(url):
        return False
    return bool(REDDIT_COMMENTS_RE.match(path)) and not is_reddit_comment(url)


def looks_like_youtube(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host in {"youtu.be", "www.youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"}


def looks_like_landing_page(url: str, title: str | None) -> bool:
    parts = urlsplit(url)
    path = (parts.path or "").strip("/")
    host = parts.netloc.lower().replace("www.", "")

    # path empty or very shallow often means "homepage/landing"
    shallow = (path == "") or (path.count("/") == 0 and len(path) <= 12)

    t = (title or "").strip().lower()
    generic_titles = {"home", "homepage", "welcome", "index", host}

    return shallow and (t in generic_titles or t == "")


def infer_kind_priority(url: str, title: str | None) -> tuple[str, int]:
    """
    Lower number = higher priority.
    Priority order requested:
    reddit comments > reddit posts > x/twitter = pocket = chrome articles > linkedin > chrome landing pages
    """
    if is_reddit_comment(url):
        return ("reddit_comment", 0)
    if is_reddit_post(url):
        return ("reddit_post", 1)
    if is_x_tweet(url):
        return ("x_twitter", 2)
    if is_pocket(url):
        return ("pocket", 2)
    if is_linkedin(url):
        return ("linkedin", 3)
    if looks_like_landing_page(url, title):
        return ("chrome_landing", 4)
    if looks_like_youtube(url):
        # your caveat: videos title-only; treat as low/medium priority bucket
        return ("youtube", 4)
    # default: treat as “chrome article”
    return ("chrome_article", 2)
