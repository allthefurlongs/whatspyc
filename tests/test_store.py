"""SQLite store tests — round-trip insert + connect-record assembly."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from whatspyc.store.store import SqliteStore


def test_message_round_trip(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "1-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "hi", "ts": 1_700_000_000_000}
    )
    rows = s.recent_messages("M0ABC")
    assert rows[0]["body"] == "hi"
    s.close()


def test_connect_record_uses_meta_and_channels(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    # `lm` matches DM `ts` units (seconds); `lp` matches post `ts` (ms).
    s.bump_meta("last_message", 1_700_000_000)
    s.set_subscription(3, True)
    s.upsert_post(3, {"ts": 1_700_000_001_000, "fc": "T3EST", "p": "hello"})
    record = s.connect_record(name="Tester", callsign="m0abc", version=0.1)
    assert record["t"] == "c"
    assert record["c"] == "M0ABC"
    assert record["lm"] == 1_700_000_000
    assert any(c["cid"] == 3 and c["lp"] == 1_700_000_001_000 for c in record["cc"])
    s.close()


def test_upsert_message_bumps_last_message_to_wire_unit(tmp_path: Path) -> None:
    """Regression: `lm` must be in the wire unit of DM `ts` — seconds —
    so the server's `ts > lm` filter excludes already-seen DMs on
    reconnect. Storing ms here (the old buggy unit) used to inflate the
    cursor past every legitimate seconds-based DM the server later sent.
    """
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "1700000000000-M0ABC", "fc": "M0ABC", "tc": "T3EST",
         "m": "hi", "ts": 1_700_000_000}
    )
    record = s.connect_record(name="Tester", callsign="t3est", version=0.1)
    assert record["lm"] == 1_700_000_000
    s.close()


def test_migrate_rewrites_ms_dm_ts_to_seconds(tmp_path: Path) -> None:
    """A pre-existing db with DM ts in ms (legacy buggy outbound from
    earlier client versions) gets rewritten to seconds on init so future
    bumps and the connect record's `lm` align with the wire unit."""
    path = tmp_path / "state.sqlite3"
    s = SqliteStore(path)
    # Forge a row with ms-magnitude ts directly via the connection
    # (bypasses the normal upsert path so we can simulate legacy data).
    with s._conn:  # type: ignore[attr-defined]
        s._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO messages(id, from_call, to_call, body, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy-id", "M0ABC", "T3EST", "old", 1_777_000_000_000),
        )
        s._conn.execute(  # type: ignore[attr-defined]
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("last_message", 1_777_000_000_000),
        )
    s.close()

    # Re-open: _migrate runs and divides ms-magnitude values by 1000.
    s2 = SqliteStore(path)
    row = next(iter(s2.recent_messages("M0ABC")), None)
    assert row is not None and row["ts"] == 1_777_000_000
    record = s2.connect_record(name="Tester", callsign="t3est", version=0.1)
    assert record["lm"] == 1_777_000_000
    s2.close()


def test_post_round_trip(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(1, True)
    s.upsert_post(1, {"ts": 100, "fc": "T3EST", "p": "first"})
    s.upsert_post(1, {"ts": 200, "fc": "T3EST", "p": "second"})
    posts = s.recent_posts(1)
    assert [p["body"] for p in posts] == ["second", "first"]
    s.close()


def test_list_dm_peers_groups_and_orders_by_recency(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    me = "M0ABC"
    # Two peers, mixed inbound and outbound, varied timestamps.
    s.upsert_message({"_id": "1", "fc": "M0FOO", "tc": me, "m": "hi", "ts": 100})
    s.upsert_message({"_id": "2", "fc": me, "tc": "M0FOO", "m": "hey", "ts": 200})
    s.upsert_message({"_id": "3", "fc": "G7BAR", "tc": me, "m": "yo", "ts": 500})
    s.upsert_message({"_id": "4", "fc": me, "tc": "G7BAR", "m": "ack", "ts": 300})

    peers = s.list_dm_peers(me)
    # Most recent peer first: G7BAR (last_ts=500) before M0FOO (last_ts=200).
    assert [p["peer"] for p in peers] == ["G7BAR", "M0FOO"]
    by_peer = {p["peer"]: p for p in peers}
    assert by_peer["M0FOO"]["count"] == 2
    assert by_peer["G7BAR"]["count"] == 2
    assert by_peer["G7BAR"]["last_ts"] == 500
    s.close()


def test_list_dm_peers_empty(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    assert s.list_dm_peers("M0ABC") == []
    s.close()


def test_lookup_message_by_lid_round_trips(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message({"_id": "1-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "hi", "ts": 100})
    s.upsert_message({"_id": "2-M0ABC", "fc": "M0ABC", "tc": "T3EST", "m": "two", "ts": 200})
    rows = s.recent_messages("M0ABC")
    # recent_messages returns newest first; both rows expose the rowid as `lid`.
    lids = {r["body"]: r["lid"] for r in rows}
    assert lids["hi"] != lids["two"]
    hit = s.lookup_message_by_lid(lids["hi"])
    assert hit is not None
    assert hit["id"] == "1-M0ABC"
    assert hit["body"] == "hi"
    assert s.lookup_message_by_lid(99_999) is None
    s.close()


def test_lookup_post_by_lid_round_trips(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(7, True)
    s.upsert_post(7, {"ts": 100, "fc": "T3EST", "p": "first"})
    s.upsert_post(7, {"ts": 200, "fc": "T3EST", "p": "second"})
    posts = s.recent_posts(7)
    by_body = {p["body"]: p for p in posts}
    hit = s.lookup_post_by_lid(by_body["first"]["lid"])
    assert hit is not None
    assert hit["channel_id"] == 7
    assert hit["ts"] == 100
    assert s.lookup_post_by_lid(99_999) is None
    s.close()


def test_post_rowid_survives_upsert_edit(tmp_path: Path) -> None:
    """Editing a post via upsert (same cid+ts) keeps the rowid stable —
    that's what makes the lid a usable handle for /editpost across
    re-renders and reconnects."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(1, True)
    s.upsert_post(1, {"ts": 100, "fc": "T3EST", "p": "original"})
    original_lid = s.recent_posts(1)[0]["lid"]
    s.upsert_post(1, {"ts": 100, "fc": "T3EST", "p": "edited", "edts": 150})
    rows = s.recent_posts(1)
    assert len(rows) == 1
    assert rows[0]["lid"] == original_lid
    assert rows[0]["body"] == "edited"
    s.close()


def test_list_dm_peers_uppercases_my_call(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message({"_id": "1", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100})
    # Stored rows use uppercase callsigns; passing lowercase should still match.
    peers = s.list_dm_peers("m0abc")
    assert [p["peer"] for p in peers] == ["M0FOO"]
    s.close()


def test_upsert_message_persists_realtime_and_received_ts(tmp_path: Path) -> None:
    """``realtime`` and ``received_ts`` are written when supplied; default
    call sites that don't pass them keep the columns NULL so existing
    behaviour is unaffected."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100},
        realtime=True,
        received_ts=200,
    )
    s.upsert_message(
        {"_id": "2-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "yo", "ts": 300},
    )
    rows = {r["id"]: r for r in s.recent_messages("M0FOO")}
    assert rows["1-M0FOO"]["realtime"] == 1
    assert rows["1-M0FOO"]["received_ts"] == 200
    assert rows["2-M0FOO"]["realtime"] is None
    assert rows["2-M0FOO"]["received_ts"] is None
    s.close()


def test_upsert_message_first_write_wins_for_receipt_columns(tmp_path: Path) -> None:
    """Receipt info describes the *first* observation. A later upsert
    (e.g. an edit batch re-upserting an existing row) must not clobber
    the original ``realtime`` / ``received_ts`` values."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100},
        realtime=True,
        received_ts=200,
    )
    s.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "edited", "ts": 100},
        realtime=False,
        received_ts=999,
    )
    row = s.recent_messages("M0FOO")[0]
    assert row["body"] == "edited"
    assert row["realtime"] == 1  # original realtime flag preserved
    assert row["received_ts"] == 200  # original receipt time preserved
    s.close()


def test_upsert_post_persists_realtime_and_received_ts(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(7, True)
    s.upsert_post(
        7,
        {"ts": 100, "fc": "M0FOO", "p": "live"},
        realtime=True,
        received_ts=150,
    )
    s.upsert_post(7, {"ts": 200, "fc": "M0FOO", "p": "from-batch"})
    rows = {r["ts"]: r for r in s.recent_posts(7)}
    assert rows[100]["realtime"] == 1
    assert rows[100]["received_ts"] == 150
    assert rows[200]["realtime"] is None
    assert rows[200]["received_ts"] is None
    s.close()


def test_mark_message_delivered_round_trips(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "1-M0ABC", "fc": "M0ABC", "tc": "M0FOO", "m": "hi", "ts": 100, "ms": 0}
    )
    matched = s.mark_message_delivered("1-M0ABC", 250)
    assert matched == 1
    row = s.lookup_message_by_id("1-M0ABC")
    assert row is not None
    assert row["msg_status"] == 1
    assert row["delivered_ts"] == 250
    # Idempotent — running it again still returns 1 (the row matches).
    assert s.mark_message_delivered("1-M0ABC", 300) == 1
    # Unknown id matches nothing.
    assert s.mark_message_delivered("nope", 0) == 0
    s.close()


def test_mark_post_delivered_resolves_via_from_call_and_ts(tmp_path: Path) -> None:
    """``cpr`` carries only ``ts``/``dts`` (no ``cid``), so the store
    locates the post by ``(from_call, ts)``. That lookup must update
    only our outbound row, not someone else's post that happens to
    share the same ts."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(7, True)
    s.set_subscription(11, True)
    s.upsert_post(7, {"ts": 1000, "fc": "M0ABC", "p": "ours"})
    s.upsert_post(11, {"ts": 1000, "fc": "M0FOO", "p": "theirs"})
    matched = s.mark_post_delivered(from_call="M0ABC", ts=1000, delivered_ts=1500)
    assert matched == 1
    rows = {r["channel_id"]: r for r in s.recent_posts(7) + s.recent_posts(11)}
    assert rows[7]["delivered_ts"] == 1500
    assert rows[11]["delivered_ts"] is None
    s.close()


def test_recent_messages_paginates_with_before_ts(tmp_path: Path) -> None:
    """`before_ts` is the cursor for scroll-back paging: pass the oldest
    already-rendered row's ts to fetch the next older page. An empty
    return signals exhaustion."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    me = "M0ABC"
    for ts in range(100, 600, 100):  # 100, 200, 300, 400, 500
        s.upsert_message(
            {"_id": f"{ts}-M0FOO", "fc": "M0FOO", "tc": me, "m": f"m{ts}", "ts": ts}
        )
    # First page: newest 2.
    page1 = s.recent_messages("M0FOO", limit=2)
    assert [r["ts"] for r in page1] == [500, 400]
    # Next page using cursor = oldest ts of page1.
    page2 = s.recent_messages("M0FOO", limit=2, before_ts=page1[-1]["ts"])
    assert [r["ts"] for r in page2] == [300, 200]
    # Final page: only one row left.
    page3 = s.recent_messages("M0FOO", limit=2, before_ts=page2[-1]["ts"])
    assert [r["ts"] for r in page3] == [100]
    # Past the end → empty.
    assert s.recent_messages("M0FOO", limit=2, before_ts=page3[-1]["ts"]) == []
    s.close()


def test_recent_posts_paginates_with_before_ts(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(7, True)
    for ts in (1000, 2000, 3000, 4000, 5000):
        s.upsert_post(7, {"ts": ts, "fc": "M0FOO", "p": f"p{ts}"})
    page1 = s.recent_posts(7, limit=2)
    assert [r["ts"] for r in page1] == [5000, 4000]
    page2 = s.recent_posts(7, limit=2, before_ts=page1[-1]["ts"])
    assert [r["ts"] for r in page2] == [3000, 2000]
    page3 = s.recent_posts(7, limit=2, before_ts=page2[-1]["ts"])
    assert [r["ts"] for r in page3] == [1000]
    assert s.recent_posts(7, limit=2, before_ts=page3[-1]["ts"]) == []
    s.close()


def test_lookup_message_by_id(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.upsert_message(
        {"_id": "1-M0FOO", "fc": "M0FOO", "tc": "M0ABC", "m": "hi", "ts": 100}
    )
    row = s.lookup_message_by_id("1-M0FOO")
    assert row is not None
    assert row["body"] == "hi"
    assert "lid" in row
    assert s.lookup_message_by_id("nope") is None
    s.close()


def test_lookup_post_by_cid_ts(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(7, True)
    s.upsert_post(7, {"ts": 100, "fc": "M0FOO", "p": "first"})
    row = s.lookup_post(7, 100)
    assert row is not None
    assert row["body"] == "first"
    assert s.lookup_post(7, 999) is None
    assert s.lookup_post(99, 100) is None
    s.close()


def test_message_emoji_list_attributes_peer_and_preserves_ours(tmp_path: Path) -> None:
    """`mem` carries no per-emoji authorship — the wire form is just a
    list of emoji strings. The store attributes new entries to the DM
    peer while keeping any rows we wrote ourselves (so our own
    `react_message` stays attributed to us across server echoes)."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    msg = "1700000000-PEER"
    # Peer reacted first; we have no local row yet, so attribute to peer.
    s.apply_message_emoji_list(msg, "PEER", ["1f44d", "1f603"], 1_700_000_000)
    rows = {r["emoji"]: r["callsign"] for r in s.list_message_emojis(msg)}
    assert rows == {"1f44d": "PEER", "1f603": "PEER"}

    # Our own outbound react writes through with my_call.
    s.upsert_message_emoji(msg, "2764", "ME", 1_700_000_001)
    # Server echoes back the new full list — ME stays attributed to us,
    # any newly-arrived emoji we didn't add gets the peer's callsign,
    # and emojis missing from the new list are dropped.
    s.apply_message_emoji_list(
        msg, "PEER", ["1f44d", "2764", "1f389"], 1_700_000_002
    )
    rows = {r["emoji"]: r["callsign"] for r in s.list_message_emojis(msg)}
    assert rows == {"1f44d": "PEER", "2764": "ME", "1f389": "PEER"}

    # Empty list = full removal.
    s.apply_message_emoji_list(msg, "PEER", [], 1_700_000_003)
    assert s.list_message_emojis(msg) == []
    s.close()


def test_message_emoji_list_bumps_last_emoji_cursor(tmp_path: Path) -> None:
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.apply_message_emoji_list("1-PEER", "PEER", ["1f44d"], 1_700_000_500)
    record = s.connect_record(name="T", callsign="ME", version=0.1)
    assert record["le"] == 1_700_000_500
    # Older ets must not regress the cursor.
    s.apply_message_emoji_list("2-PEER", "PEER", ["1f603"], 1_700_000_100)
    record = s.connect_record(name="T", callsign="ME", version=0.1)
    assert record["le"] == 1_700_000_500
    s.close()


def test_post_emoji_upsert_and_remove(tmp_path: Path) -> None:
    """Real-time `cpem` add/remove paths. Server injects `fc` (the
    reactor's callsign) before relaying — see post_emoji_handler in
    wps.py."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(7, True)
    s.upsert_post(7, {"ts": 100, "fc": "M0FOO", "p": "hello"})
    s.upsert_post_emoji(7, 100, "1f44d", "M1ABC", 1_700_000_000)
    s.upsert_post_emoji(7, 100, "1f44d", "M2DEF", 1_700_000_001)
    s.upsert_post_emoji(7, 100, "2764", "M1ABC", 1_700_000_002)
    rows = s.list_post_emojis(7, 100)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {
        ("1f44d", "M1ABC"),
        ("1f44d", "M2DEF"),
        ("2764", "M1ABC"),
    }
    s.remove_post_emoji(7, 100, "1f44d", "M1ABC")
    rows = s.list_post_emojis(7, 100)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {
        ("1f44d", "M2DEF"),
        ("2764", "M1ABC"),
    }
    # `last_emoji` tracks per-channel via the `channels` table.
    chans = {c["cid"]: c for c in s.list_channels()}
    assert chans[7]["last_emoji"] == 1_700_000_002
    s.close()


def test_post_emoji_batch_replaces_full_state(tmp_path: Path) -> None:
    """`cpemb` always carries the *latest complete* per-post emoji
    state (per the protocol spec). The store replaces the whole row
    set on apply so a removal between connects doesn't leave
    orphans."""
    s = SqliteStore(tmp_path / "state.sqlite3")
    s.set_subscription(5, True)
    s.upsert_post(5, {"ts": 200, "fc": "M0FOO", "p": "hi"})
    # Pre-existing rows from a previous session.
    s.upsert_post_emoji(5, 200, "1f44d", "STALE1", 1_700_000_000)
    s.upsert_post_emoji(5, 200, "1f603", "STALE2", 1_700_000_000)
    # Connect-batch arrives with the canonical state.
    s.apply_post_emoji_batch(
        5,
        200,
        [
            {"e": "1f44d", "c": ["M1ABC", "M2DEF"]},
            {"e": "2764", "c": ["M3GHI"]},
        ],
        1_700_000_900,
    )
    rows = s.list_post_emojis(5, 200)
    assert {(r["emoji"], r["callsign"]) for r in rows} == {
        ("1f44d", "M1ABC"),
        ("1f44d", "M2DEF"),
        ("2764", "M3GHI"),
    }
    chans = {c["cid"]: c for c in s.list_channels()}
    assert chans[5]["last_emoji"] == 1_700_000_900
    s.close()


def test_message_emojis_callsign_column_added_on_old_db(tmp_path: Path) -> None:
    """Old dbs predate the callsign column on message_emojis. Re-opening
    such a db must add it via ALTER rather than failing on the first
    insert from an emoji handler."""
    path = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE message_emojis (
            msg_id    TEXT NOT NULL,
            emoji     TEXT NOT NULL,
            emoji_ts  INTEGER NOT NULL,
            PRIMARY KEY (msg_id, emoji)
        );
        """
    )
    conn.execute(
        "INSERT INTO message_emojis(msg_id, emoji, emoji_ts) VALUES (?, ?, ?)",
        ("legacy-1", "1f44d", 1_700_000_000),
    )
    conn.commit()
    conn.close()

    s = SqliteStore(path)
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(message_emojis)")}
    assert "callsign" in cols
    # Legacy row survives, with empty-string callsign default.
    rows = s.list_message_emojis("legacy-1")
    assert rows == [{"emoji": "1f44d", "callsign": "", "emoji_ts": 1_700_000_000}]
    s.close()


def test_schema_migration_adds_new_columns_to_pre_existing_db(tmp_path: Path) -> None:
    """Old dbs predate the receipt/delivery columns. Re-opening such a
    db must add them via ``ALTER TABLE`` rather than failing to upsert."""
    path = tmp_path / "state.sqlite3"
    # Build a "v0" schema by hand — just messages and posts without the
    # new columns. SqliteStore's _migrate must add them on next open.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            from_call TEXT NOT NULL,
            to_call TEXT NOT NULL,
            body TEXT NOT NULL,
            ts INTEGER NOT NULL,
            edit_ts INTEGER,
            reply_id TEXT,
            msg_status INTEGER
        );
        CREATE TABLE posts (
            channel_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            from_call TEXT NOT NULL,
            body TEXT NOT NULL,
            edit_ts INTEGER,
            reply_ts INTEGER,
            reply_from TEXT,
            PRIMARY KEY (channel_id, ts)
        );
        """
    )
    conn.execute(
        "INSERT INTO messages(id, from_call, to_call, body, ts) VALUES (?, ?, ?, ?, ?)",
        ("legacy-1", "M0FOO", "M0ABC", "hi", 100),
    )
    conn.commit()
    conn.close()

    s = SqliteStore(path)
    cols_messages = {
        r["name"] for r in s._conn.execute("PRAGMA table_info(messages)")
    }
    cols_posts = {r["name"] for r in s._conn.execute("PRAGMA table_info(posts)")}
    for c in ("received_ts", "realtime", "delivered_ts"):
        assert c in cols_messages, f"messages missing {c}"
        assert c in cols_posts, f"posts missing {c}"
    # Legacy row survives untouched, with new columns NULL.
    row = s.lookup_message_by_id("legacy-1")
    assert row is not None
    assert row["body"] == "hi"
    assert row["received_ts"] is None
    assert row["realtime"] is None
    assert row["delivered_ts"] is None
    s.close()
