"""Unit tests for the searchable emoji catalog."""

from __future__ import annotations

from whatspyc.ui import emoji_for_display, emoji_to_wire
from whatspyc.ui.emoji_catalog import (
    EXTRA_ALIASES,
    EmojiEntry,
    build_catalog,
    by_char,
    entries_in,
    groups,
    search,
)


def test_build_catalog_returns_non_empty_entries() -> None:
    catalog = build_catalog()
    assert len(catalog) > 1000
    sample = catalog[0]
    assert isinstance(sample, EmojiEntry)
    assert sample.char
    assert sample.name
    assert sample.keywords  # never empty — at least the name's tokens


def test_build_catalog_is_cached() -> None:
    a = build_catalog()
    b = build_catalog()
    assert a is b  # identity, not just equality


def test_search_canonical_name_match() -> None:
    results = [e.char for e in search("rocket", limit=10)]
    assert "🚀" in results
    # Canonical-name exact match should rank first.
    assert results[0] == "🚀"


def test_search_smile_returns_smiling_faces() -> None:
    results = [e.char for e in search("smile", limit=20)]
    # Several common smiling faces — at least one should appear.
    assert any(c in results for c in ("😄", "😃", "🙂", "😊"))


def test_search_extra_aliases_match() -> None:
    # "yes" is not part of any canonical CLDR name but is in EXTRA_ALIASES
    # for both ✅ and 👍.
    results = [e.char for e in search("yes", limit=20)]
    assert "👍" in results
    assert "✅" in results


def test_search_empty_string_returns_empty() -> None:
    assert search("") == []
    assert search("   ") == []


def test_search_no_match_returns_empty() -> None:
    assert search("xxxxxxxxxnothere") == []


def test_search_respects_limit() -> None:
    # "a" is a very common substring in keyword lists — easily exceeds 5.
    results = search("a", limit=5)
    assert len(results) == 5


def test_search_is_case_insensitive() -> None:
    a = [e.char for e in search("Fire", limit=5)]
    b = [e.char for e in search("fire", limit=5)]
    assert a == b


def test_extra_aliases_chars_all_in_catalog() -> None:
    # If we list a char in EXTRA_ALIASES, it should be a fully-qualified
    # entry the catalog actually exposes — otherwise the alias is dead.
    catalog_chars = {e.char for e in build_catalog()}
    missing = [c for c in EXTRA_ALIASES if c not in catalog_chars]
    assert not missing, f"EXTRA_ALIASES chars not in catalog: {missing}"


def test_groups_in_cldr_order() -> None:
    gs = groups()
    names = [g for g, _ in gs]
    # CLDR order: Smileys, People, Animals, Food, Travel, Activities,
    # Objects, Symbols, Flags. (Component is dropped.)
    assert names == [
        "Smileys & Emotion",
        "People & Body",
        "Animals & Nature",
        "Food & Drink",
        "Travel & Places",
        "Activities",
        "Objects",
        "Symbols",
        "Flags",
    ]
    # Each group has at least one subgroup.
    for _, subs in gs:
        assert subs


def test_entries_in_group_returns_only_that_group() -> None:
    smileys = entries_in("Smileys & Emotion")
    assert smileys
    assert all(e.group == "Smileys & Emotion" for e in smileys)
    # CLDR order — `order` strictly increases.
    orders = [e.order for e in smileys]
    assert orders == sorted(orders)


def test_entries_in_subgroup_filters_correctly() -> None:
    open_hands = entries_in("People & Body", "hand-fingers-open")
    assert open_hands
    assert all(
        e.group == "People & Body" and e.subgroup == "hand-fingers-open"
        for e in open_hands
    )


def test_entries_in_unknown_returns_empty() -> None:
    assert entries_in("Not A Group") == []
    assert entries_in("Smileys & Emotion", "not-a-subgroup") == []


def test_by_char_lookup() -> None:
    e = by_char("🔥")
    assert e is not None
    assert e.char == "🔥"
    assert e.name == "fire"
    assert by_char("not-an-emoji") is None


def test_skin_tone_variants_excluded() -> None:
    # 1F3FB-1F3FF should not appear in any catalog entry's char.
    skin = {chr(c) for c in range(0x1F3FB, 0x1F3FF + 1)}
    for e in build_catalog():
        assert not (set(e.char) & skin), f"skin-tone variant leaked: {e.char!r}"


def test_catalog_round_trips_through_wire_layer() -> None:
    # Every catalog entry should survive emoji_to_wire → emoji_for_display
    # for single-codepoint chars; multi-codepoint sequences pass through
    # untouched on both sides (per emoji_to_wire's docstring).
    for entry in build_catalog()[:100]:
        c = entry.char
        wire = emoji_to_wire(c)
        if len(c) == 1:
            assert wire == format(ord(c), "x")
            assert emoji_for_display(wire) == c
        else:
            assert wire == c
            assert emoji_for_display(wire) == c
