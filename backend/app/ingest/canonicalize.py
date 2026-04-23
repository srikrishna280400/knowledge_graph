import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

DROP_QUERY_PREFIXES = ("utm_",)
DROP_QUERY_KEYS = {
    "ref", "ref_src", "ref_url",
    "fbclid", "gclid", "igshid",
}

MD_LINK_RE = re.compile(r"^\[(https?://[^\]]+)\]\((https?://[^)]+)\)$")


def unwrap_markdown_link(s: str) -> str:
    s = (s or "").strip()
    m = MD_LINK_RE.match(s)
    if m:
        # keep the actual target ( ... )(TARGET )
        return m.group(2).strip()
    return s


def canonicalize_url(url: str) -> str:
    url = unwrap_markdown_link(url)

    parts = urlsplit(url)

    query_pairs = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if any(k.startswith(p) for p in DROP_QUERY_PREFIXES):
            continue
        if k in DROP_QUERY_KEYS:
            continue
        query_pairs.append((k, v))

    query = urlencode(query_pairs, doseq=True)

    # Normalize: drop fragment (#...) for dedupe purposes
    fragment = ""

    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, fragment))
