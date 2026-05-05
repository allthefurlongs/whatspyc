"""Searchable Unicode emoji catalog used by the TUI emoji picker.

Membership and CLDR group/subgroup ordering come from
``whatspyc/data/emoji_groups.json`` — a flat list bundled with the
package, derived from unicode.org's ``emoji-test.txt``. Names and
aliases come from the ``emoji`` PyPI package's ``EMOJI_DATA`` table
(looked up per char). Skin-tone variants are excluded from the bundled
data so the picker stays browsable; users wanting a specific skin tone
can paste a literal char into the hex / literal fallback Input.

To refresh after a Unicode emoji release, fetch the latest
``https://unicode.org/Public/emoji/<ver>/emoji-test.txt`` and rerun the
small parser at the bottom of this module's ``__main__`` (kept as a
docstring example to avoid shipping a generator script).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True, slots=True)
class EmojiEntry:
    char: str
    name: str
    keywords: tuple[str, ...]
    group: str
    subgroup: str
    order: int  # index in CLDR order, useful for stable sorting


# Common synonyms where the canonical CLDR name doesn't match the way
# people actually search. Keep additions short and high-signal.
EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "👍": ("yes", "ok", "approve", "like", "good"),
    "👎": ("no", "disapprove", "dislike", "bad"),
    "❤️": ("love", "heart"),
    "💔": ("broken", "heartbreak"),
    "🔥": ("fire", "lit", "hot"),
    "💯": ("hundred", "perfect", "100"),
    "😂": ("laugh", "lol", "haha"),
    "🤣": ("rofl", "laugh"),
    "😢": ("sad", "cry", "tear"),
    "😭": ("cry", "sob"),
    "😠": ("angry", "mad"),
    "😡": ("angry", "rage", "furious"),
    "🎉": ("party", "celebrate", "yay"),
    "🥳": ("party", "celebrate", "birthday"),
    "🙏": ("please", "thanks", "pray"),
    "👏": ("clap", "applause"),
    "🤔": ("think", "hmm", "ponder"),
    "👀": ("eyes", "look", "watch"),
    "🚀": ("rocket", "launch", "ship"),
    "✅": ("check", "yes", "ok", "done"),
    "❌": ("cross", "no", "fail"),
    "💀": ("skull", "dead", "rip"),
    "😎": ("cool", "shades", "sunglasses"),
    "🤝": ("handshake", "deal"),
    "👋": ("wave", "hi", "hello", "bye"),
    "😇": ("angel", "innocent"),
    "😅": ("sweat", "phew", "nervous"),
    "😴": ("sleep", "tired", "zzz"),
    "🙄": ("eyeroll", "annoyed"),
    "😮": ("wow", "surprised", "shock"),
    "😱": ("scream", "shocked"),
    "🤯": ("mindblown", "shocked"),
    "🤷": ("shrug", "idk"),
    "🫶": ("heart", "hands"),
    "👌": ("ok", "perfect"),
    "🤞": ("fingers", "crossed", "luck"),
    "🥺": ("pleading", "puppy"),
    "😊": ("smile", "happy", "blush"),
    "🙂": ("smile", "happy"),
    "😄": ("smile", "happy", "grin"),
    "😃": ("smile", "happy", "grin"),
    "🤖": ("robot", "bot"),
    "💩": ("poop", "shit"),
    "🍕": ("pizza", "food"),
    "🍺": ("beer", "drink"),
    "☕": ("coffee", "drink"),
    "🎂": ("cake", "birthday"),
    "🤬": ("swear", "curse", "angry"),
    "😏": ("smirk", "smug"),
    "💪": ("strong", "muscle", "flex"),
}


_catalog: tuple[EmojiEntry, ...] | None = None
_by_char: dict[str, EmojiEntry] | None = None
_groups_index: list[tuple[str, list[str]]] | None = None
# (group, [subgroup, ...]) preserving CLDR order, for tab construction.


def _load_groups_data() -> list[dict]:
    raw = resources.files("whatspyc.data").joinpath("emoji_groups.json").read_text(
        encoding="utf-8"
    )
    return json.loads(raw)


def _build_keywords(char: str, name: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    kw: list[str] = name.split()
    for alias in aliases:
        stripped = alias.strip(":")
        if stripped:
            kw.extend(stripped.replace("_", " ").split())
    kw.extend(EXTRA_ALIASES.get(char, ()))
    seen: set[str] = set()
    out: list[str] = []
    for token in kw:
        t = token.lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return tuple(out)


def build_catalog() -> tuple[EmojiEntry, ...]:
    """Return the emoji catalog, building (and caching) it on first call.

    Walks the bundled CLDR-ordered group data, looks up each char's name
    and aliases in ``emoji.EMOJI_DATA``, and returns one ``EmojiEntry``
    per fully-qualified non-skin-tone-variant emoji.
    """
    global _catalog, _by_char, _groups_index
    if _catalog is not None:
        return _catalog

    import emoji as _emoji_pkg

    entries: list[EmojiEntry] = []
    groups_seen: dict[str, list[str]] = {}
    groups_order: list[str] = []
    order = 0

    for bucket in _load_groups_data():
        group = bucket["group"]
        subgroup = bucket["subgroup"]
        if group not in groups_seen:
            groups_seen[group] = []
            groups_order.append(group)
        if subgroup not in groups_seen[group]:
            groups_seen[group].append(subgroup)

        for char in bucket["chars"]:
            data = _emoji_pkg.EMOJI_DATA.get(char, {})
            canonical = data.get("en", "").strip(":")
            if not canonical:
                # Fall back to a stable placeholder so we don't drop the
                # entry — names should always exist in the package, but
                # being defensive avoids a silent membership mismatch.
                canonical = " ".join(format(ord(c), "x") for c in char)
            name = canonical.replace("_", " ")
            aliases = tuple(data.get("alias", ()))
            keywords = _build_keywords(char, name, aliases)
            entries.append(
                EmojiEntry(
                    char=char,
                    name=name,
                    keywords=keywords,
                    group=group,
                    subgroup=subgroup,
                    order=order,
                )
            )
            order += 1

    _catalog = tuple(entries)
    _by_char = {e.char: e for e in entries}
    _groups_index = [(g, list(groups_seen[g])) for g in groups_order]
    return _catalog


def by_char(char: str) -> EmojiEntry | None:
    """Lookup an entry by its literal char (cached)."""
    if _by_char is None:
        build_catalog()
    assert _by_char is not None
    return _by_char.get(char)


def groups() -> list[tuple[str, list[str]]]:
    """Return ``[(group_name, [subgroup_name, ...]), ...]`` in CLDR order."""
    if _groups_index is None:
        build_catalog()
    assert _groups_index is not None
    return list(_groups_index)


def entries_in(group: str, subgroup: str | None = None) -> list[EmojiEntry]:
    """Return entries for a group (and optional subgroup) in CLDR order."""
    catalog = build_catalog()
    if subgroup is None:
        return [e for e in catalog if e.group == group]
    return [e for e in catalog if e.group == group and e.subgroup == subgroup]


def search(query: str, limit: int = 200) -> list[EmojiEntry]:
    """Return up to ``limit`` catalog entries matching ``query``.

    Empty query → empty list (callers fall back to their own curated
    set or the active group). Otherwise rank by:
      0. canonical name equals the query
      1. canonical name starts with the query
      2. any keyword equals the query
      3. any keyword starts with the query
      4. canonical name contains the query
      5. any keyword contains the query
    Within a tier, original catalog order wins (mirrors CLDR order).
    """
    q = query.strip().lower()
    if not q:
        return []

    catalog = build_catalog()
    buckets: list[list[EmojiEntry]] = [[] for _ in range(6)]
    for e in catalog:
        nl = e.name.lower()
        if nl == q:
            buckets[0].append(e)
        elif nl.startswith(q):
            buckets[1].append(e)
        elif any(k == q for k in e.keywords):
            buckets[2].append(e)
        elif any(k.startswith(q) for k in e.keywords):
            buckets[3].append(e)
        elif q in nl:
            buckets[4].append(e)
        elif any(q in k for k in e.keywords):
            buckets[5].append(e)

    results: list[EmojiEntry] = []
    for bucket in buckets:
        for e in bucket:
            results.append(e)
            if len(results) >= limit:
                return results
    return results
