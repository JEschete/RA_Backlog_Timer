"""HowLongToBeat search, wrapped around the pure matching logic."""
from __future__ import annotations

from howlongtobeatpy import HowLongToBeat, SearchModifiers

from ..config import log
from ..matching import (
    build_variants,
    classify,
    comment_for,
    normalize_title,
    score_candidate,
)
from ..models import LookupResult
from .http import with_retries

# Item #13: one client for the process, not one per game. Threshold 0 returns
# everything so the scoring in matching.py decides, not the library.
_CLIENT = HowLongToBeat(0.0)


def cache_key(title: str, system: str) -> str:
    return f"{title}|{system}"


async def search(title: str, system: str = "") -> LookupResult:
    """Find the best HLTB entry for an RA title.

    Never raises: transport problems come back as status="error" so the caller
    can decline to cache them permanently (item #3).
    """
    clean = normalize_title(title)
    variants = build_variants(clean)

    best_match = None
    best_score = float("-inf")
    best_term = ""

    try:
        for variant in variants:
            results = await with_retries(
                lambda v=variant: _CLIENT.async_search(
                    v.text, search_modifiers=SearchModifiers.HIDE_DLC),
                label=f"HLTB search {variant.text!r}",
            )
            if not results:
                continue

            for candidate in results:
                raw = score_candidate(
                    variant.text, candidate.game_name, candidate.similarity)
                # Weighting is what stops a truncated variant from winning on a
                # spurious exact match (item #9).
                weighted = raw * variant.weight
                if weighted > best_score:
                    best_score = weighted
                    best_match = candidate
                    best_term = variant.text

    except Exception as exc:
        log.debug("HLTB lookup failed for %r: %s", title, exc)
        return LookupResult(status="error", error=f"{type(exc).__name__}: {exc}",
                            comment=f"Lookup error: {exc}")

    if best_match is None:
        return LookupResult(status="no_match", quality="none",
                            comment=comment_for("none", None, 0.0))

    exact = any(v.text.lower().strip() == best_match.game_name.lower().strip()
                for v in variants)
    quality = classify(best_score, best_match.similarity, exact)

    beat = _positive(best_match.main_story) or _positive(best_match.main_extra)
    complete = _positive(best_match.completionist)

    log.debug("Matched %r -> %r via %r (score %.0f, %s)",
              title, best_match.game_name, best_term, best_score, quality)

    return LookupResult(
        status="ok",
        beat=beat,
        complete=complete,
        hltb_name=best_match.game_name,
        similarity=float(best_match.similarity or 0.0),
        quality=quality,
        comment=comment_for(quality, best_match.game_name,
                            float(best_match.similarity or 0.0)),
    )


def _positive(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if f > 0 else None
