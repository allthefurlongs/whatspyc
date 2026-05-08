"""SQLite-backed local store of WPS state.

Used to feed accurate timestamps into the type-`c` connect handshake so the
server only sends deltas, and to keep a usable local history of messages,
posts, channel subscriptions, and ham name lookups.

Synchronous SQLite access via the stdlib driver is fine here — calls are
short, and ``WpsClient`` runs them on the event loop's executor only when it
matters.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_schema_sql())
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Schema migration

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema for existing dbs.

        ``CREATE TABLE IF NOT EXISTS`` only creates a missing table; it
        does not reconcile columns. Pre-existing dbs need an explicit
        ``ALTER TABLE ... ADD COLUMN`` per missing column. SQLite ignores
        the column when the schema-script CREATE already produced it on a
        fresh db, so this is the only path that runs both for upgraders
        and is a no-op on fresh installs.
        """
        for table, column, decl in (
            ("messages", "received_ts", "INTEGER"),
            ("messages", "realtime", "INTEGER"),
            ("messages", "delivered_ts", "INTEGER"),
            ("posts", "received_ts", "INTEGER"),
            ("posts", "realtime", "INTEGER"),
            ("posts", "delivered_ts", "INTEGER"),
            ("posts", "ets", "INTEGER"),
            ("posts", "is_gap", "INTEGER NOT NULL DEFAULT 0"),
            ("posts", "at_calls", "TEXT"),
            ("message_emojis", "callsign", "TEXT NOT NULL DEFAULT ''"),
            ("channels", "last_read_ts", "INTEGER NOT NULL DEFAULT 0"),
        ):
            cols = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in cols:
                with self._conn:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                    )

        # The WhatsPac protocol stores DM ``ts`` in seconds (the web
        # client sends ``Math.round(Date.now()/1e3)`` and renders with
        # ``ts*1e3``); channel-post ``ts`` and DM ``edts`` stay in ms.
        # Earlier versions of this client incorrectly used ms for
        # outbound DMs, polluting the local store with ms-magnitude
        # values and leaving ``last_message`` stuck at a ms cursor that
        # blocks every seconds-based DM the server later tries to
        # deliver. Rewrite any ms-magnitude DM ``ts`` (and the matching
        # ``last_message`` cursor) back to seconds. ``1e12`` is the
        # cliff (year 2001 in ms), so any value at or above it can only
        # be ms. Posts and DM edit-cursors stay untouched — they're
        # ms-native by the web-client convention.
        _MS_THRESHOLD = 1_000_000_000_000
        with self._conn:
            self._conn.execute(
                "UPDATE messages SET ts = ts / 1000 WHERE ts >= ?",
                (_MS_THRESHOLD,),
            )
            self._conn.execute(
                "UPDATE meta SET value = value / 1000"
                " WHERE key = 'last_message' AND value >= ?",
                (_MS_THRESHOLD,),
            )

    # ------------------------------------------------------------------
    # Connect-record support
    # ------------------------------------------------------------------

    def connect_record(self, name: str, callsign: str, version: float) -> dict:
        """Build a type-`c` client record using stored timestamps.

        Both ``led`` (last-edit cursor) and ``le`` (last-reaction
        cursor) are floored to ``max(ts)`` over the rows they scope —
        channel posts for the per-channel pair, DMs for the top-level
        pair. Reason: the server's replay filters (see
        ``wps/db.py:dbGetPostEdits`` / ``dbGetPostEmojis`` and the
        equivalent message paths) are ``edts > led AND ts <= lp`` and
        ``ets > le AND ts <= lp``. ``last_edit`` / ``last_emoji`` are
        only ever bumped when we *observe* the matching wire event
        (`cped`/`cpedb`/`med`/`medb` for edits, `cpem`/`cpemb`/`mem`
        for reactions) — a fresh subscription, or a channel where
        we've simply never been online while an edit or reaction
        happened, leaves the cursor at 0. The server then replays
        every historical edit/reaction on next connect (gigabytes on
        long-lived channels). Edits and reactions can only happen
        *after* the post is created (``edts > ts``, ``ets > ts``), and
        `cpb` always delivers the current body and inline reaction
        state, so anything with ``edts <= max(ts)`` or
        ``ets <= max(ts)`` is already baked into the bodies we hold;
        flooring suppresses the firehose without risking a missed
        update.

        Per-channel floor is gap-aware. When a channel was paged-
        subscribed (``pc`` rather than full-history), the server marks
        the first delivered post with ``g=1`` (``wps/wps.py``,
        ``unpause_channel_handler``). Posts older than that marker
        sit in a gap that will never be filled — but pre-gap posts
        from earlier sessions may still be in our store, and edits or
        reactions to *those* pre-gap posts can occur during the gap
        window. Flooring at ``max(post.ts)`` (latest post-gap ts)
        would suppress the replay of those legitimate edits to
        pre-gap posts. So in the gap case we floor at
        ``max(g.ts)`` — the gap-marker's ts — which is < any
        post-gap edit-ts that isn't already baked into the cpb
        bodies, and >= any edit-ts in the gap window (which we
        couldn't apply anyway since the target posts are missing).
        Without a gap we keep the existing ``max(post.ts)`` floor
        which is tight against the firehose.
        """
        cur = self._conn.execute("SELECT key, value FROM meta")
        meta = {row["key"]: row["value"] for row in cur.fetchall()}
        cur = self._conn.execute(
            "SELECT c.cid, c.last_post, c.last_emoji, c.last_edit,"
            "       COALESCE(MAX(p.ts), 0) AS max_post_ts,"
            "       COALESCE(MAX(p.edit_ts), 0) AS max_post_edts,"
            "       COALESCE(MAX(p.ets), 0) AS max_post_ets,"
            "       COALESCE(MAX(CASE WHEN p.is_gap = 1 THEN p.ts END), 0)"
            "         AS max_gap_ts"
            "  FROM channels c"
            "  LEFT JOIN posts p ON p.channel_id = c.cid"
            " WHERE c.subscribed = 1"
            " GROUP BY c.cid, c.last_post, c.last_emoji, c.last_edit"
        )
        channels = []
        for r in cur.fetchall():
            if r["max_gap_ts"] > 0:
                led = max(r["last_edit"], r["max_post_edts"], r["max_gap_ts"])
                le = max(r["last_emoji"], r["max_post_ets"], r["max_gap_ts"])
            else:
                led = max(r["last_edit"], r["max_post_ts"])
                le = max(r["last_emoji"], r["max_post_ts"])
            channels.append(
                {
                    "cid": r["cid"],
                    "lp": r["last_post"],
                    "le": le,
                    "led": led,
                }
            )
        max_msg_ts = self._conn.execute(
            "SELECT COALESCE(MAX(ts), 0) AS m FROM messages"
        ).fetchone()["m"]
        return {
            "t": "c",
            "n": name,
            "c": callsign.upper(),
            "lm": meta.get("last_message", 0),
            "le": max(meta.get("last_emoji", 0), max_msg_ts),
            "led": max(meta.get("last_edit", 0), max_msg_ts),
            "lhts": meta.get("last_ham_ts", 0),
            "v": version,
            "cc": channels,
        }

    def bump_meta(self, key: str, value: int) -> None:
        """Set ``meta[key]`` to ``max(existing, value)``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)",
                (key, value),
            )

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def upsert_message(
        self,
        m: dict,
        *,
        realtime: bool | None = None,
        received_ts: int | None = None,
        delivered_ts: int | None = None,
    ) -> None:
        msg_id = m.get("_id") or f"{m['ts']}-{m['fc']}"
        rt = None if realtime is None else (1 if realtime else 0)
        with self._conn:
            self._conn.execute(
                "INSERT INTO messages(id, from_call, to_call, body, ts, edit_ts,"
                " reply_id, msg_status, received_ts, realtime, delivered_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET body=excluded.body,"
                " edit_ts=COALESCE(excluded.edit_ts, messages.edit_ts),"
                # Receipt info is a property of the *first* time we saw
                # the row — never overwrite it on a later upsert (e.g. an
                # edit batch re-upserting an existing row).
                " received_ts=COALESCE(messages.received_ts, excluded.received_ts),"
                " realtime=COALESCE(messages.realtime, excluded.realtime),"
                # delivered_ts is set by the `mr` ack handler when we get
                # an authoritative server timestamp; on inbound paths
                # carrying our own callsign (a row coming back from the
                # server, e.g. cpb backfill or a different client
                # instance's send) the caller seeds a synthetic value so
                # the row doesn't render as still-pending. Either way,
                # never let a later upsert clobber an existing value.
                " delivered_ts=COALESCE(messages.delivered_ts, excluded.delivered_ts)",
                (
                    msg_id,
                    m.get("fc"),
                    m.get("tc"),
                    m.get("m"),
                    m.get("ts"),
                    m.get("edts"),
                    m.get("r"),
                    m.get("ms"),
                    received_ts,
                    rt,
                    delivered_ts,
                ),
            )
        # DM `ts` is **seconds** since epoch on the wire (the web client
        # sends `Math.round(Date.now()/1e3)`). `lm` in the type-`c`
        # connect record uses the same unit. Storing what the wire gave
        # us keeps `bump_meta` and the server's `ts > lm` filter in
        # agreement; mixing in ms-magnitude values would push the cursor
        # past every legitimate seconds-based DM and look like "0 new
        # DMs" forever.
        if (ts := m.get("ts")) is not None:
            self.bump_meta("last_message", int(ts))

    def mark_message_delivered(self, msg_id: str, delivered_ts: int) -> int:
        """Flip ``msg_status = 1`` and record ``delivered_ts`` for the
        message acked by a `mr` frame. Returns the count of rows updated
        — `0` is fine and just means the ack arrived for a row we don't
        have a copy of (e.g. cleared `state_dir` between send and ack).
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE messages SET msg_status = 1, delivered_ts = ?"
                " WHERE id = ?",
                (int(delivered_ts), msg_id),
            )
        return cur.rowcount

    def apply_message_edit(self, msg_id: str, body: str, edit_ts: int) -> int:
        """UPDATE-only: rewrite the body of an existing DM and bump its
        ``edit_ts``. Returns the rowcount — ``0`` means the row isn't in
        our store (e.g. an edit landed for a message that predates our
        local cursor) and the caller can decide whether to render
        anything for it.

        Receipt metadata (``received_ts`` / ``realtime``) is *not*
        touched: it describes the first observation, not later edits.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE messages SET body = ?,"
                " edit_ts = MAX(COALESCE(edit_ts, 0), ?)"
                " WHERE id = ?",
                (body, int(edit_ts), msg_id),
            )
        return cur.rowcount

    def recent_messages(
        self, peer: str, limit: int = 50, *, before_ts: int | None = None
    ) -> list[dict]:
        """Return up to ``limit`` recent DM rows with ``peer``, newest first.

        ``before_ts`` is the cursor for paginated scroll-back: pass the
        oldest already-rendered row's ``ts`` to fetch the next older
        page. ``None`` (the default) returns the most recent page.
        """
        if before_ts is None:
            cur = self._conn.execute(
                "SELECT rowid AS lid, * FROM messages WHERE from_call = ? OR to_call = ? "
                "ORDER BY ts DESC LIMIT ?",
                (peer, peer, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT rowid AS lid, * FROM messages WHERE (from_call = ? OR to_call = ?) "
                "AND ts < ? ORDER BY ts DESC LIMIT ?",
                (peer, peer, int(before_ts), limit),
            )
        return [dict(r) for r in cur.fetchall()]

    def lookup_message_by_lid(self, lid: int) -> dict | None:
        """Return the message row for a local short id (SQLite rowid), or None.

        The rowid is the integer the UI exposes via /editdm and friends so
        users don't have to type the server's `{ts}-{fc}` ``_id``.
        """
        cur = self._conn.execute(
            "SELECT rowid AS lid, * FROM messages WHERE rowid = ?", (int(lid),)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def lookup_message_by_id(self, msg_id: str) -> dict | None:
        """Return the message row for a server ``_id`` (`{ts}-{fc}`), or None.

        Used by the verbose render path to recover the lid + receipt
        columns for a freshly-arrived `m` frame.
        """
        cur = self._conn.execute(
            "SELECT rowid AS lid, * FROM messages WHERE id = ?", (msg_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def apply_message_emoji_list(
        self,
        msg_id: str,
        peer_call: str,
        emojis: list[str],
        emoji_ts: int,
    ) -> None:
        """Replace the full emoji set for ``msg_id``.

        Wire reality: `mem` carries no per-emoji authorship (just a list
        of emoji strings — DM is 1-to-1, but the server stores set
        semantics and the protocol doesn't surface "who reacted"). We
        attribute locally: existing rows keep their callsign (so an
        emoji we wrote from ``react_message`` stays attributed to us);
        any newly-arrived emoji that we don't have a row for is
        attributed to the DM peer.

        Bumps ``meta.last_emoji`` so the connect record's ``le`` field
        excludes already-seen emoji updates on the next reconnect.
        """
        peer = peer_call.upper()
        existing_by_emoji = {
            row["emoji"]: row["callsign"]
            for row in self._conn.execute(
                "SELECT emoji, callsign FROM message_emojis WHERE msg_id = ?",
                (msg_id,),
            )
        }
        new_set = set(emojis)
        with self._conn:
            if new_set:
                placeholders = ",".join("?" * len(new_set))
                self._conn.execute(
                    f"DELETE FROM message_emojis WHERE msg_id = ?"
                    f" AND emoji NOT IN ({placeholders})",
                    [msg_id, *new_set],
                )
            else:
                self._conn.execute(
                    "DELETE FROM message_emojis WHERE msg_id = ?", (msg_id,)
                )
            for e in emojis:
                if e in existing_by_emoji:
                    self._conn.execute(
                        "UPDATE message_emojis SET emoji_ts = MAX(emoji_ts, ?)"
                        " WHERE msg_id = ? AND emoji = ?",
                        (int(emoji_ts), msg_id, e),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO message_emojis(msg_id, emoji, callsign, emoji_ts)"
                        " VALUES (?, ?, ?, ?)",
                        (msg_id, e, peer, int(emoji_ts)),
                    )
        self.bump_meta("last_emoji", int(emoji_ts))

    def upsert_message_emoji(
        self, msg_id: str, emoji: str, callsign: str, emoji_ts: int
    ) -> None:
        """Insert (or refresh ``emoji_ts`` on) one local emoji row.

        Used by the outbound ``react_message`` path so the user's own
        reaction is visible immediately — the WPS server doesn't echo
        DM reactions back to the sender.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO message_emojis(msg_id, emoji, callsign, emoji_ts)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(msg_id, emoji) DO UPDATE SET"
                " callsign = excluded.callsign,"
                " emoji_ts = MAX(message_emojis.emoji_ts, excluded.emoji_ts)",
                (msg_id, emoji, callsign.upper(), int(emoji_ts)),
            )
        self.bump_meta("last_emoji", int(emoji_ts))

    def remove_message_emoji(self, msg_id: str, emoji: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM message_emojis WHERE msg_id = ? AND emoji = ?",
                (msg_id, emoji),
            )

    def list_message_emojis(self, msg_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT emoji, callsign, emoji_ts FROM message_emojis"
            " WHERE msg_id = ? ORDER BY emoji_ts ASC, emoji ASC",
            (msg_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_message_emojis_for_ids(
        self, msg_ids: list[str]
    ) -> dict[str, list[dict]]:
        """Bulk variant of :meth:`list_message_emojis`.

        Returns a dict keyed by ``msg_id``; ids with no reactions are
        absent (callers must default to ``[]``). One SQL round trip
        replaces N — meaningful when seeding a freshly-mounted message
        ListView with the configured backfill count, since otherwise
        every visible row triggers its own query.
        """
        if not msg_ids:
            return {}
        # Dedup while preserving order to keep placeholder count = ids.
        seen: set[str] = set()
        unique: list[str] = []
        for mid in msg_ids:
            if mid in seen:
                continue
            seen.add(mid)
            unique.append(mid)
        placeholders = ",".join("?" for _ in unique)
        cur = self._conn.execute(
            "SELECT msg_id, emoji, callsign, emoji_ts FROM message_emojis"
            f" WHERE msg_id IN ({placeholders})"
            " ORDER BY emoji_ts ASC, emoji ASC",
            tuple(unique),
        )
        out: dict[str, list[dict]] = {}
        for row in cur.fetchall():
            d = dict(row)
            mid = d.pop("msg_id")
            out.setdefault(mid, []).append(d)
        return out

    def list_dm_peers(self, my_call: str) -> list[dict]:
        """Return distinct DM peers ordered by most recent message.

        Each row: ``{"peer": CALLSIGN, "last_ts": <ms>, "count": N}``.
        """
        me = my_call.upper()
        cur = self._conn.execute(
            "SELECT CASE WHEN from_call = ? THEN to_call ELSE from_call END AS peer, "
            "       MAX(ts) AS last_ts, COUNT(*) AS count "
            "FROM messages WHERE from_call = ? OR to_call = ? "
            "GROUP BY peer ORDER BY last_ts DESC",
            (me, me, me),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------

    def upsert_post(
        self,
        channel_id: int,
        p: dict,
        *,
        realtime: bool | None = None,
        received_ts: int | None = None,
        delivered_ts: int | None = None,
    ) -> None:
        rt = None if realtime is None else (1 if realtime else 0)
        is_gap = 1 if p.get("g") else 0
        # `at` arrives as a JSON list on the wire; persist as a JSON
        # string so the column round-trips losslessly. Empty / missing
        # → NULL so render code can distinguish "no mentions" from
        # "explicit empty list" (the wire never sends an empty list).
        at_raw = p.get("at")
        if isinstance(at_raw, list) and at_raw:
            import json as _json

            at_calls = _json.dumps([str(c).upper() for c in at_raw])
        else:
            at_calls = None
        with self._conn:
            self._conn.execute(
                "INSERT INTO posts(channel_id, ts, from_call, body, edit_ts, ets,"
                " reply_ts, reply_from, received_ts, realtime, delivered_ts, is_gap,"
                " at_calls)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(channel_id, ts) DO UPDATE SET body=excluded.body,"
                " edit_ts=COALESCE(excluded.edit_ts, posts.edit_ts),"
                # ets is server-monotonic; bump rather than overwrite so a
                # later cpb that omits the field can't unset an observed
                # reaction-timestamp baseline.
                " ets=MAX(COALESCE(posts.ets, 0), COALESCE(excluded.ets, 0)),"
                # Same first-write-wins rule as messages — receipt info
                # describes the *original* observation, not a later edit.
                " received_ts=COALESCE(posts.received_ts, excluded.received_ts),"
                " realtime=COALESCE(posts.realtime, excluded.realtime),"
                # See `upsert_message` — delivered_ts is preserved across
                # upserts so a later backfill can't unset an authoritative
                # ack value, and an authoritative ack via mark_post_delivered
                # always overwrites the synthetic seed since that path uses
                # a plain UPDATE.
                " delivered_ts=COALESCE(posts.delivered_ts, excluded.delivered_ts),"
                # Gap flag is sticky — once a post has been observed as a
                # gap-marker on any code path, leave it set.
                " is_gap=MAX(posts.is_gap, excluded.is_gap),"
                # at_calls is set on the original `cp`; cped/cpb-replay
                # frames don't carry it, so a later upsert that omits it
                # can't unset an observed mention list.
                " at_calls=COALESCE(posts.at_calls, excluded.at_calls)",
                (
                    channel_id,
                    p["ts"],
                    p.get("fc"),
                    p.get("p"),
                    p.get("edts"),
                    p.get("ets"),
                    p.get("rts"),
                    p.get("rfc"),
                    received_ts,
                    rt,
                    delivered_ts,
                    is_gap,
                    at_calls,
                ),
            )
            self._conn.execute(
                "UPDATE channels SET last_post = MAX(last_post, ?) WHERE cid = ?",
                (p["ts"], channel_id),
            )

    def apply_post_edit(
        self, channel_id: int, ts: int, body: str, edit_ts: int
    ) -> int:
        """UPDATE-only: rewrite the body of an existing post and bump
        its ``edit_ts``. Returns the rowcount.

        Receipt metadata is preserved (same first-write-wins rule as
        :meth:`apply_message_edit`). The caller is responsible for
        bumping the per-channel ``last_edit`` cursor via
        :meth:`bump_channel_last_edit`.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE posts SET body = ?,"
                " edit_ts = MAX(COALESCE(edit_ts, 0), ?)"
                " WHERE channel_id = ? AND ts = ?",
                (body, int(edit_ts), int(channel_id), int(ts)),
            )
        return cur.rowcount

    def bump_channel_last_edit(self, channel_id: int, edit_ts: int) -> None:
        """Bump the per-channel ``last_edit`` cursor — the value that
        feeds the ``led`` field of the type-`c` connect record so the
        server only re-sends post edits we haven't seen yet.

        Creates the ``channels`` row if missing (a `cped` can arrive
        for a channel we haven't subscribed to from this client yet —
        the user may have sub'd from the web client and is now reading
        from whatspyc).
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO channels(cid, last_edit) VALUES (?, ?)"
                " ON CONFLICT(cid) DO UPDATE SET"
                " last_edit = MAX(last_edit, excluded.last_edit)",
                (int(channel_id), int(edit_ts)),
            )

    def mark_post_delivered(
        self, *, from_call: str, ts: int, delivered_ts: int
    ) -> int:
        """Record ``delivered_ts`` for the post acked by a `cpr` frame.

        ``cpr`` carries only ``ts``/``dts``, not ``cid`` — but the post is
        keyed on ``(cid, ts)``, so we locate it by ``(from_call, ts)``
        instead. The user's outbound posts at the millisecond resolution
        are unique per author, so this resolves cleanly.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE posts SET delivered_ts = ?"
                " WHERE from_call = ? AND ts = ?",
                (int(delivered_ts), from_call.upper(), int(ts)),
            )
        return cur.rowcount

    def recent_posts(
        self, channel_id: int, limit: int = 50, *, before_ts: int | None = None
    ) -> list[dict]:
        """Return up to ``limit`` recent posts in ``channel_id``, newest first.

        ``before_ts`` is the cursor for paginated scroll-back: pass the
        oldest already-rendered post's ``ts`` to fetch the next older
        page. ``None`` returns the most recent page.
        """
        if before_ts is None:
            cur = self._conn.execute(
                "SELECT rowid AS lid, * FROM posts WHERE channel_id = ? "
                "ORDER BY ts DESC LIMIT ?",
                (channel_id, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT rowid AS lid, * FROM posts WHERE channel_id = ? "
                "AND ts < ? ORDER BY ts DESC LIMIT ?",
                (channel_id, int(before_ts), limit),
            )
        return [dict(r) for r in cur.fetchall()]

    def lookup_post_by_lid(self, lid: int) -> dict | None:
        """Return the post row for a local short id (SQLite rowid), or None.

        Posts have no server-side identifier (they're keyed on cid+ts) so
        the rowid is also what /editpost takes — looking it up here gives
        the (channel_id, ts) pair the cped frame needs.
        """
        cur = self._conn.execute(
            "SELECT rowid AS lid, * FROM posts WHERE rowid = ?", (int(lid),)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def lookup_post(self, channel_id: int, ts: int) -> dict | None:
        """Return the post row keyed by ``(channel_id, ts)``, or None.

        Used by the verbose render path to recover the lid + receipt
        columns for a freshly-arrived `cp` frame.
        """
        cur = self._conn.execute(
            "SELECT rowid AS lid, * FROM posts WHERE channel_id = ? AND ts = ?",
            (int(channel_id), int(ts)),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_post_emoji(
        self,
        channel_id: int,
        ts: int,
        emoji: str,
        callsign: str,
        emoji_ts: int,
    ) -> None:
        """Add (or refresh ``emoji_ts`` on) one ``(cid, ts, emoji,
        callsign)`` row. Used by the real-time ``cpem`` add path and
        by the user's own outbound ``react_post``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO post_emojis(channel_id, ts, emoji, callsign, emoji_ts)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(channel_id, ts, emoji, callsign) DO UPDATE SET"
                " emoji_ts = MAX(post_emojis.emoji_ts, excluded.emoji_ts)",
                (
                    int(channel_id),
                    int(ts),
                    emoji,
                    callsign.upper(),
                    int(emoji_ts),
                ),
            )
            self._conn.execute(
                "UPDATE posts SET ets = MAX(COALESCE(ets, 0), ?)"
                " WHERE channel_id = ? AND ts = ?",
                (int(emoji_ts), int(channel_id), int(ts)),
            )
            self._conn.execute(
                "INSERT INTO channels(cid, last_emoji) VALUES (?, ?)"
                " ON CONFLICT(cid) DO UPDATE SET"
                " last_emoji = MAX(last_emoji, excluded.last_emoji)",
                (int(channel_id), int(emoji_ts)),
            )

    def remove_post_emoji(
        self, channel_id: int, ts: int, emoji: str, callsign: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM post_emojis WHERE channel_id = ? AND ts = ?"
                " AND emoji = ? AND callsign = ?",
                (int(channel_id), int(ts), emoji, callsign.upper()),
            )

    def apply_post_emoji_batch(
        self,
        channel_id: int,
        ts: int,
        entries: list[dict],
        emoji_ts: int,
    ) -> None:
        """Replace all emoji rows for ``(channel_id, ts)`` from a
        ``cpemb`` group entry.

        ``entries`` is the post's ``e`` array — each item is
        ``{"e": <emoji>, "c": [<callsign>, ...]}`` per the protocol
        (see CHANNELS.md, ``cpemb``). The wire form here *does* carry
        per-callsign attribution, unlike DMs.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM post_emojis WHERE channel_id = ? AND ts = ?",
                (int(channel_id), int(ts)),
            )
            for entry in entries:
                e = entry.get("e")
                callsigns = entry.get("c") or []
                if not isinstance(e, str):
                    continue
                for c in callsigns:
                    if not isinstance(c, str) or not c:
                        continue
                    self._conn.execute(
                        "INSERT OR IGNORE INTO post_emojis"
                        "(channel_id, ts, emoji, callsign, emoji_ts)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            int(channel_id),
                            int(ts),
                            e,
                            c.upper(),
                            int(emoji_ts),
                        ),
                    )
            self._conn.execute(
                "UPDATE posts SET ets = MAX(COALESCE(ets, 0), ?)"
                " WHERE channel_id = ? AND ts = ?",
                (int(emoji_ts), int(channel_id), int(ts)),
            )
            self._conn.execute(
                "INSERT INTO channels(cid, last_emoji) VALUES (?, ?)"
                " ON CONFLICT(cid) DO UPDATE SET"
                " last_emoji = MAX(last_emoji, excluded.last_emoji)",
                (int(channel_id), int(emoji_ts)),
            )

    def list_post_emojis(self, channel_id: int, ts: int) -> list[dict]:
        cur = self._conn.execute(
            "SELECT emoji, callsign, emoji_ts FROM post_emojis"
            " WHERE channel_id = ? AND ts = ?"
            " ORDER BY emoji_ts ASC, callsign ASC, emoji ASC",
            (int(channel_id), int(ts)),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_post_emojis_for_keys(
        self, channel_id: int, ts_list: list[int]
    ) -> dict[int, list[dict]]:
        """Bulk variant of :meth:`list_post_emojis` for one channel.

        Returns a dict keyed by post ``ts``; entries with no reactions
        are absent. Callers must default missing keys to ``[]``. Used by
        the TUI's initial backfill to avoid one SQLite round trip per
        post in a freshly-mounted channel view.
        """
        if not ts_list:
            return {}
        seen: set[int] = set()
        unique: list[int] = []
        for t in ts_list:
            i = int(t)
            if i in seen:
                continue
            seen.add(i)
            unique.append(i)
        placeholders = ",".join("?" for _ in unique)
        cur = self._conn.execute(
            "SELECT ts, emoji, callsign, emoji_ts FROM post_emojis"
            f" WHERE channel_id = ? AND ts IN ({placeholders})"
            " ORDER BY emoji_ts ASC, callsign ASC, emoji ASC",
            (int(channel_id), *unique),
        )
        out: dict[int, list[dict]] = {}
        for row in cur.fetchall():
            d = dict(row)
            ts_val = int(d.pop("ts"))
            out.setdefault(ts_val, []).append(d)
        return out

    def lookup_post_by_from_ts(self, from_call: str, ts: int) -> dict | None:
        """Return the post row keyed by ``(from_call, ts)``, or None.

        ``cpr`` acks omit ``cid``, so resolving back to the original row
        from a delivery confirmation has to go through the author. The
        user's outbound posts at ms resolution are unique per author, so
        this resolves cleanly for the only case that needs it (rendering
        the ack against our own outbound row).
        """
        cur = self._conn.execute(
            "SELECT rowid AS lid, * FROM posts WHERE from_call = ? AND ts = ?",
            (from_call.upper(), int(ts)),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def set_subscription(self, channel_id: int, subscribed: bool) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO channels(cid, subscribed) VALUES (?, ?) "
                "ON CONFLICT(cid) DO UPDATE SET subscribed = excluded.subscribed",
                (channel_id, 1 if subscribed else 0),
            )

    def list_channels(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM channels ORDER BY cid")
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Unread cursors (channels + DMs)
    # ------------------------------------------------------------------
    #
    # The UI's unread badge for a target is the number of inbound rows
    # newer than its `last_read_ts` cursor. Channels store the cursor on
    # the `channels` row (ms, post.ts unit); DM peers use the `dm_read`
    # table (seconds, message.ts unit per gotcha 10). Activation in the
    # UI calls ``mark_*_read`` to advance the cursor to the latest row
    # currently in the store; inbound rows that arrive later have ts >
    # cursor and so count toward the next session's unread badge.
    #
    # Outbound rows (from_call == my_call) are excluded from the count
    # — sending a message doesn't make it "unread to yourself".

    def mark_channel_read(self, channel_id: int) -> None:
        """Advance ``channels.last_read_ts`` to the most recent post.ts.
        Creates the channels row if missing (the user may have read
        without having ever subscribed via this client)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO channels(cid, last_read_ts) VALUES (?,"
                " COALESCE((SELECT MAX(ts) FROM posts WHERE channel_id = ?), 0))"
                " ON CONFLICT(cid) DO UPDATE SET last_read_ts = MAX("
                " channels.last_read_ts,"
                " COALESCE((SELECT MAX(ts) FROM posts WHERE channel_id = ?), 0))",
                (int(channel_id), int(channel_id), int(channel_id)),
            )

    def mark_dm_read(self, peer: str) -> None:
        """Advance ``dm_read.last_read_ts`` for ``peer`` to the most
        recent DM ts in either direction with that peer."""
        p = peer.upper()
        with self._conn:
            self._conn.execute(
                "INSERT INTO dm_read(peer, last_read_ts) VALUES (?,"
                " COALESCE((SELECT MAX(ts) FROM messages"
                "   WHERE from_call = ? OR to_call = ?), 0))"
                " ON CONFLICT(peer) DO UPDATE SET last_read_ts = MAX("
                " dm_read.last_read_ts,"
                " COALESCE((SELECT MAX(ts) FROM messages"
                "   WHERE from_call = ? OR to_call = ?), 0))",
                (p, p, p, p, p),
            )

    def unread_post_count(self, channel_id: int, my_call: str) -> int:
        """Number of inbound (not-from-me) posts in ``channel_id`` with
        ts > the channel's last_read_ts."""
        me = my_call.upper()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM posts"
            " WHERE channel_id = ?"
            "   AND from_call != ?"
            "   AND ts > COALESCE("
            "     (SELECT last_read_ts FROM channels WHERE cid = ?), 0)",
            (int(channel_id), me, int(channel_id)),
        ).fetchone()
        return int(row["n"])

    def unread_dm_count(self, peer: str, my_call: str) -> int:
        """Number of inbound DMs from ``peer`` with ts > peer's
        last_read_ts. Outbound DMs to ``peer`` are excluded."""
        p = peer.upper()
        me = my_call.upper()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages"
            " WHERE from_call = ?"
            "   AND to_call = ?"
            "   AND ts > COALESCE("
            "     (SELECT last_read_ts FROM dm_read WHERE peer = ?), 0)",
            (p, me, p),
        ).fetchone()
        return int(row["n"])

    def unread_post_counts_all(self, my_call: str) -> dict[int, int]:
        """Bulk variant for startup: ``{cid: unread}`` for every channel
        with at least one unread inbound post. Channels with zero unread
        are absent from the result."""
        me = my_call.upper()
        cur = self._conn.execute(
            "SELECT p.channel_id AS cid, COUNT(*) AS n"
            " FROM posts p"
            " LEFT JOIN channels c ON c.cid = p.channel_id"
            " WHERE p.from_call != ?"
            "   AND p.ts > COALESCE(c.last_read_ts, 0)"
            " GROUP BY p.channel_id",
            (me,),
        )
        return {int(r["cid"]): int(r["n"]) for r in cur.fetchall()}

    def unread_dm_counts_all(self, my_call: str) -> dict[str, int]:
        """Bulk variant for startup: ``{peer: unread}`` for every DM peer
        with at least one unread inbound DM."""
        me = my_call.upper()
        cur = self._conn.execute(
            "SELECT m.from_call AS peer, COUNT(*) AS n"
            " FROM messages m"
            " LEFT JOIN dm_read d ON d.peer = m.from_call"
            " WHERE m.to_call = ?"
            "   AND m.from_call != ?"
            "   AND m.ts > COALESCE(d.last_read_ts, 0)"
            " GROUP BY m.from_call",
            (me, me),
        )
        return {str(r["peer"]).upper(): int(r["n"]) for r in cur.fetchall()}

    # ------------------------------------------------------------------
    # Hams
    # ------------------------------------------------------------------

    def upsert_ham(self, callsign: str, name: str, ts: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO hams(callsign, name, last_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(callsign) DO UPDATE SET name=excluded.name, "
                "last_ts=MAX(last_ts, excluded.last_ts)",
                (callsign.upper(), name, ts),
            )
        self.bump_meta("last_ham_ts", ts)

    def lookup_ham(self, callsign: str) -> dict | None:
        cur = self._conn.execute("SELECT * FROM hams WHERE callsign = ?", (callsign.upper(),))
        row = cur.fetchone()
        return dict(row) if row else None


def _schema_sql() -> str:
    return resources.files("whatspyc.store").joinpath("schema.sql").read_text(encoding="utf-8")
