from urllib.parse import urlsplit, unquote

def title_from_url(url: str) -> str:
    parts = urlsplit(url)
    path = (parts.path or "").strip("/")

    if not path:
        return parts.netloc or "Untitled"

    # last path segment tends to be the slug
    slug = path.split("/")[-1]
    slug = slug.split("?")[0]
    slug = slug.split("#")[0]

    # drop common extensions
    for ext in (".html", ".htm", ".php", ".asp", ".aspx"):
        if slug.lower().endswith(ext):
            slug = slug[: -len(ext)]
            break

    # decode %xx escapes and replace separators
    slug = unquote(slug)  # percent-decoding [web:185]
    slug = slug.replace("-", " ").replace("_", " ").strip()

    if not slug:
        return parts.netloc or "Untitled"

    # light cleanup
    return " ".join(w.capitalize() for w in slug.split())
