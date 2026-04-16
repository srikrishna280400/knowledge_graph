from __future__ import annotations
import math


def clamp(lo: int, x: int, hi: int) -> int:
    return max(lo, min(x, hi))


def p_for_len(L: int) -> float:
    if L <= 4_000:
        return 0.25
    if L <= 12_000:
        return 0.35
    if L <= 40_000:
        return 0.45
    if L <= 120_000:
        return 0.55
    return 0.60


def sample_text(text: str, allowed_chars: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    L = len(text)
    if L <= allowed_chars:
        return text

    # Always: first 20% + last 10% + 5 middle chunks
    first_n = int(allowed_chars * 0.20)
    last_n = int(allowed_chars * 0.10)
    middle_budget = allowed_chars - first_n - last_n
    chunks = 5
    each = max(200, middle_budget // chunks)

    head = text[:first_n]
    tail = text[-last_n:] if last_n > 0 else ""

    # middle sampling: evenly spaced windows
    middle_parts = []
    if middle_budget > 0:
        start_min = first_n
        end_max = max(first_n, L - last_n)
        span = max(1, end_max - start_min)
        for i in range(chunks):
            center = start_min + int((i + 0.5) * (span / chunks))
            s = clamp(0, center - each // 2, L)
            e = clamp(0, s + each, L)
            middle_parts.append(text[s:e])

    out = "\n\n".join(
        [head.strip(), *[m.strip() for m in middle_parts if m.strip()], tail.strip()]
    ).strip()
    return out


def build_prompt_content(title: str | None, extracted_text: str | None, min_chars=1200, max_chars=60000) -> tuple[str, dict]:
    title = (title or "").strip()
    text = (extracted_text or "").strip()

    if not text:
        # title-only path handled by caller; still return something minimal
        return f"TITLE: {title}\nCONTENT:\n", {"mode": "title_only", "allowed_chars": 0, "sent_chars": 0}

    L = len(text)
    p = p_for_len(L)
    allowed = clamp(min_chars, int(p * L), max_chars)
    sampled = sample_text(text, allowed)

    payload = f"TITLE: {title}\nCONTENT:\n{sampled}".strip()
    return payload, {"mode": "sampled", "L": L, "p": p, "allowed_chars": allowed, "sent_chars": len(payload)}
