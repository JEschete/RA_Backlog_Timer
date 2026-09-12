"""Tests for title normalization and match scoring (item #15).

The `, The` cases below are the regression net for item #1.
"""
from __future__ import annotations

import pytest

from ra_backlog.matching import (
    build_variants,
    classify,
    normalize_title,
    score_candidate,
    strip_diacritics,
)


class TestArticleSwap:
    """Item #1: RA alphabetizes as "Name, The", including before a subtitle."""

    @pytest.mark.parametrize("raw,expected", [
        # The regression that motivated the fix: article before a colon.
        ("Legend of Zelda, The: A Link to the Past",
         "The Legend of Zelda: A Link to the Past"),
        ("Legend of Zelda, The: Ocarina of Time",
         "The Legend of Zelda: Ocarina of Time"),
        ("Legend of Zelda, The: Majora's Mask",
         "The Legend of Zelda: Majora's Mask"),
        # Trailing article still works (the only case the old code handled).
        ("Legend of Zelda, The", "The Legend of Zelda"),
        ("Adventures of Batman & Robin, The",
         "The Adventures of Batman & Robin"),
        ("Lion King, The", "The Lion King"),
        # Other articles.
        ("Bug's Life, A", "A Bug's Life"),
        ("American Tail, An", "An American Tail"),
        # Combined with region codes.
        ("Legend of Zelda, The: A Link to the Past (USA)",
         "The Legend of Zelda: A Link to the Past"),
    ])
    def test_article_moves_to_front(self, raw, expected):
        assert normalize_title(raw) == expected

    @pytest.mark.parametrize("raw", [
        "Chrono Trigger",
        "Secret of Mana",
        "Super Mario World",
        "Sonic, Tails and Knuckles",   # comma, but no article
    ])
    def test_titles_without_articles_are_untouched(self, raw):
        assert normalize_title(raw) == raw


class TestTagStripping:
    @pytest.mark.parametrize("raw,expected", [
        ("~Hack~ Super Mario Bros.", "Super Mario Bros."),
        ("~Homebrew~ Micro Mages", "Micro Mages"),
        ("~Prototype~ Sonic Crackers", "Sonic Crackers"),
        ("Mega Man X [Subset - Bonus]", "Mega Man X"),
        ("Contra (USA)", "Contra"),
        ("Contra (Japan)", "Contra"),
        ("Castlevania (En,Fr,De)", "Castlevania"),
        ("Final Fantasy VII (Disc 1)", "Final Fantasy VII"),
        ("Street Fighter II (Rev 1)", "Street Fighter II"),
        ("Doom (v1.1)", "Doom"),
    ])
    def test_strips(self, raw, expected):
        assert normalize_title(raw) == expected


class TestDiacritics:
    @pytest.mark.parametrize("raw,expected", [
        ("Pokémon Red Version", "Pokemon Red Version"),
        ("Ōkami", "Okami"),
        ("Pokémon HeartGold", "Pokemon HeartGold"),
    ])
    def test_folds(self, raw, expected):
        assert normalize_title(raw) == expected

    def test_strip_diacritics_is_general(self):
        assert strip_diacritics("àéîõü") == "aeiou"


class TestVariants:
    def test_deduplicates(self):
        """Item #12: the same string must not be searched twice."""
        variants = build_variants("Chrono Trigger")
        assert len(variants) == len({v.text.lower() for v in variants})

    def test_pipe_alternates_are_equal_weight(self):
        variants = build_variants("Pokemon HeartGold | SoulSilver")
        texts = [v.text for v in variants]
        assert "Pokemon HeartGold" in texts
        assert "SoulSilver" in texts
        assert all(v.weight == 1.0 for v in variants)

    def test_base_title_is_downweighted(self):
        """Item #9: the truncated fallback must not outrank the full title."""
        variants = build_variants("Castlevania: Symphony of the Night")
        full = next(v for v in variants if v.text.startswith("Castlevania:"))
        base = next(v for v in variants if v.text == "Castlevania")
        assert full.weight == 1.0
        assert base.weight < full.weight

    def test_version_suffix_variant(self):
        texts = [v.text for v in build_variants("Pokemon Red Version")]
        assert "Pokemon Red" in texts


class TestScoring:
    def test_exact_beats_everything(self):
        exact = score_candidate("Contra", "Contra", 1.0)
        partial = score_candidate("Contra", "Contra Force", 0.8)
        assert exact > partial

    def test_sequel_penalty(self):
        """Searching "Aladdin" should not return "Aladdin III"."""
        plain = score_candidate("Aladdin", "Aladdin", 1.0)
        sequel = score_candidate("Aladdin", "Aladdin III", 0.9)
        assert plain > sequel

    def test_sequel_penalty_not_applied_when_asked_for(self):
        asked = score_candidate("Final Fantasy VII", "Final Fantasy VII", 1.0)
        assert asked == pytest.approx(1000.0)

    @pytest.mark.parametrize("title", ["V-Rally", "I Have No Mouth"])
    def test_non_sequels_are_not_penalised(self, title):
        """A trailing-anchored pattern keeps "V-Rally" out of sequel territory."""
        assert score_candidate(title, title, 1.0) == pytest.approx(1000.0)

    def test_weighted_base_title_loses_to_full_match(self):
        """Item #9, end to end through the weighting."""
        variants = {v.text: v.weight for v in
                    build_variants("Castlevania: Symphony of the Night")}
        full = score_candidate(
            "Castlevania: Symphony of the Night",
            "Castlevania: Symphony of the Night (1997)", 0.9,
        ) * variants["Castlevania: Symphony of the Night"]
        base = score_candidate("Castlevania", "Castlevania", 1.0) * variants["Castlevania"]
        assert full > base


class TestClassify:
    def test_exact(self):
        assert classify(1000, 1.0, exact=True) == "exact"

    def test_thresholds(self):
        assert classify(600, 0.9, exact=False) == "fuzzy"
        assert classify(300, 0.7, exact=False) == "loose"
        assert classify(50, 0.3, exact=False) == "poor"
