"""Title normalization and HLTB match scoring.

Pure functions: no I/O, no network, no globals. Everything here is directly
unit-testable, which is the point -- the `, The` bug (item #1) survived for as
long as it did because this logic was welded to a network call.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# --- Title cleanup patterns -------------------------------------------------

# ~Hack~, ~Homebrew~, ~Prototype~, ~Demo~, ~Unlicensed~, ~Translation~ ...
_RA_TAG = re.compile(r"^~[^~]+~\s*")
_SUBSET = re.compile(r"\[Subset\s*-\s*[^\]]+\]")
_BRACKET = re.compile(r"\[[^\]]*\]")

_REGION = re.compile(
    r"\((?:USA|Europe|Japan|World|Australia|Korea|China|Brazil|Canada|Asia"
    r"|Spain|France|Germany|Italy|Netherlands|Sweden|Norway|Denmark|Finland"
    r"|En|Fr|De|Es|It|Nl|Pt|Sv|No|Da|Fi|Ja|Ko|Zh|J|U|E|A|K"
    r"|(?:[A-Za-z]{2}\s*,\s*)+[A-Za-z]{2})\)",
    re.IGNORECASE,
)
_VERSION = re.compile(
    r"\((?:Rev\s*[A-Z0-9]*|v\s*\d+(?:\.\d+)*|Beta\s*\d*|Proto(?:type)?|Sample"
    r"|Alt|Unl|Virtual\s*Console|PSN|XBLA|WiiWare|eShop|Switch\s*Online"
    r"|Arcade|Aftermarket)\)",
    re.IGNORECASE,
)
_DISC = re.compile(r"\((?:Disc|Disk|CD|Side|Tape)\s*\w+\)", re.IGNORECASE)

# Item #1. RA alphabetizes as "Name, The" -- and crucially the article can sit
# mid-string, before a subtitle colon:
#     "Legend of Zelda, The: A Link to the Past"
# The old code only tested `title.endswith(', The')`, so every subtitled game
# (a large slice of the RA library) was left unnormalized and never matched.
_ARTICLE = re.compile(r"^(.*?),\s*(The|A|An)(\s*:|\s*$)", re.IGNORECASE)

# A trailing sequel marker. Anchored to end-of-title so "V-Rally" and
# "I Have No Mouth..." are not mistaken for numbered sequels.
_SEQUEL_TAIL = re.compile(r"\b(\d+|I{2,3}|IV|VI{0,3}|IX|XI{0,2}|X)\s*$", re.IGNORECASE)

_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "and", "&", "-", "edition", "remastered",
     "hd", "definitive", "deluxe", "version", "remake", "collection"}
)


def strip_diacritics(text: str) -> str:
    """e -> e, o -> o, u -> u, and every other combining mark.

    Replaces the old hand-maintained list of three character pairs.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_title(title: str) -> str:
    """Reduce an RA title to something HowLongToBeat is likely to recognise."""
    clean = title

    clean = _RA_TAG.sub("", clean)
    clean = _SUBSET.sub("", clean)
    clean = _BRACKET.sub("", clean)
    clean = _REGION.sub("", clean)
    clean = _VERSION.sub("", clean)
    clean = _DISC.sub("", clean)

    # Collapse whitespace before the article swap so ", The :" cannot hide.
    clean = re.sub(r"\s+", " ", clean).strip()

    # "Legend of Zelda, The: Link's Awakening" -> "The Legend of Zelda: Link's Awakening"
    m = _ARTICLE.match(clean)
    if m:
        head, article, tail = m.group(1), m.group(2), m.group(3)
        rest = clean[m.end():]
        joiner = ":" if ":" in tail else ""
        clean = f"{article} {head}{joiner}{rest}"

    clean = strip_diacritics(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


@dataclass(frozen=True)
class Variant:
    """A search string plus how much we trust a match found through it."""
    text: str
    weight: float


def build_variants(clean_title: str) -> list[Variant]:
    """Ordered, de-duplicated search variants (item #12).

    Weights exist because of item #9: the base-title fallback used to be
    described as "lower priority" but was scored identically to the full title,
    so searching "Castlevania: Symphony of the Night" could return plain
    "Castlevania" on an exact-match bonus. Truncated variants now have to be
    substantially better to win.
    """
    variants: list[Variant] = []

    def add(text: str, weight: float) -> None:
        text = text.strip()
        if not text:
            return
        for i, existing in enumerate(variants):
            if existing.text.lower() == text.lower():
                if weight > existing.weight:
                    variants[i] = Variant(existing.text, weight)
                return
        variants.append(Variant(text, weight))

    # Pipe-separated alternates carry equal authority: "HeartGold | SoulSilver".
    if " | " in clean_title:
        for part in clean_title.split(" | "):
            add(part, 1.0)
    else:
        add(clean_title, 1.0)

    # "Pokemon FireRed Version" -> "Pokemon FireRed"
    for v in list(variants):
        if v.text.lower().endswith(" version"):
            add(v.text[: -len(" version")], 0.97)

    # Base title before a subtitle separator. Deliberately weak.
    for sep in (":", " - "):
        if sep in clean_title:
            add(clean_title.split(sep)[0], 0.5)
            break

    return variants


def score_candidate(search_term: str, candidate_name: str, similarity: float) -> float:
    """Raw score for one HLTB result against one search term."""
    cand = candidate_name.lower().strip()
    term = search_term.lower().strip()

    if cand == term:
        score = 1000.0
    elif term in cand or cand in term:
        score = 500.0 + similarity * 100.0
    else:
        score = similarity * 100.0

    # Don't let "Aladdin" match "Aladdin III".
    term_seq = bool(_SEQUEL_TAIL.search(term))
    cand_seq = bool(_SEQUEL_TAIL.search(cand))
    if cand_seq and not term_seq:
        score -= 300.0

    extra = set(cand.split()) - set(term.split()) - _STOPWORDS
    score -= len(extra) * 15.0

    return score


def classify(score: float, similarity: float, exact: bool) -> str:
    if exact:
        return "exact"
    if score >= 500:
        return "fuzzy"
    if score >= 200:
        return "loose"
    return "poor"


def comment_for(quality: str, name: str | None, similarity: float) -> str | None:
    if quality == "exact":
        return None
    if quality == "fuzzy":
        return f"Fuzzy match: {name}"
    if quality == "loose":
        return f"Loose match ({similarity:.0%}): {name}"
    if quality == "none":
        return "No HLTB match found"
    return f"Poor match ({similarity:.0%}): {name} - VERIFY"


SYSTEM_SEARCH_HINTS = {
    "Genesis/Mega Drive": "Genesis",
    "SNES/Super Famicom": "SNES",
    "NES/Famicom": "NES",
    "Game Boy Advance": "GBA",
    "Game Boy Color": "GBC",
    "Game Boy": "Game Boy",
    "Nintendo 64": "N64",
    "Nintendo DS": "DS",
    "PlayStation": "PlayStation",
    "PlayStation 2": "PS2",
    "PlayStation Portable": "PSP",
    "GameCube": "GameCube",
}
