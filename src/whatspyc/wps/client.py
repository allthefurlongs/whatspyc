"""High-level WPS application client.

Owns an ``AsyncByteStream`` (any transport), runs the WPS handshake
(callsign line, then type-`c`), reads frames from the codec, and dispatches
them through callbacks.

Send helpers translate ergonomic Python args into wire-format JSON. The
codec adds ``\\r\\n`` framing and optional compression.

Construction takes a *stream factory* rather than a single stream so the
client can transparently reconnect after the link drops.

Three link-health features run in the background while connected:

* periodic ``{"t":"k"}`` keep-alives at ``keepalive_interval`` seconds
  (set to ``None`` to disable). Default 540s (9 min) matches the web
  client's ``keepAliveIntervalMinutes``;
* an application-level silence guard: if the time since the last
  user-initiated send exceeds ``keepalive_max_minutes`` minutes (default
  240, matching the web client's hardcoded ``re``) the link is closed
  and auto-reconnect is suppressed. Set to ``None`` to disable.
  Keep-alive frames don't reset the guard — only real outbound traffic
  does, mirroring the web client's ``ne("RESET")`` semantics;
* automatic reconnect on EOF / read error, with exponential backoff
  (``auto_reconnect``, default off — opt-in). Backoff doubles from
  ``reconnect_initial_backoff`` (default 2 s) up to
  ``reconnect_max_backoff`` (default 60 s) between attempts.
  ``reconnect_max_retries`` caps the number of attempts; ``0`` (the
  default) means retry forever.

All three surface state through ``on_event`` as synthetic dicts:
``{"t": "_disconnect"}``, ``{"t": "_reconnecting", "attempt": N,
"delay": s}``, ``{"t": "_reconnected"}``,
``{"t": "_reconnect_giveup", "attempts": N}``,
``{"t": "_silence_disconnect", "minutes": N}``.

Per-row delivery-timeout tracking runs alongside: every outbound DM /
post schedules a one-shot task that fires after ``delivery_timeout_s``
seconds, re-checks the local row's ``delivered_ts``, and emits
``{"t": "_delivery_timeout", "kind": "dm"|"post", ...}`` if the ack
still hasn't arrived. The matching ``mr`` / ``cpr`` handlers cancel the
task as a fast path; the store-check is the source of truth so an
ack-then-timer race does the right thing. ``close`` cancels every
pending task. Set ``delivery_timeout_s = None`` to disable the feature
entirely (timers are never scheduled).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from whatspyc.store.store import SqliteStore
from whatspyc.transport.base import AsyncByteStream
from whatspyc.ui import emoji_to_wire
from whatspyc.wps import codec
from whatspyc.wps.hop_script import HopStep, ProgressFn, run_connect_script

logger = logging.getLogger(__name__)

CLIENT_VERSION = 0.92
HandlerFn = Callable[[dict], Awaitable[None]]
StreamFactory = Callable[[], AsyncByteStream]


class WpsClient:
    def __init__(
        self,
        stream_factory: StreamFactory,
        store: SqliteStore,
        *,
        my_call: str,
        name: str,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        keepalive_interval: float | None = 540.0,
        keepalive_max_minutes: int | None = 240,
        auto_reconnect: bool = False,
        reconnect_initial_backoff: float = 2.0,
        reconnect_max_backoff: float = 60.0,
        reconnect_max_retries: int = 0,
        connect_script: list[HopStep] | None = None,
        hop_progress: ProgressFn | None = None,
        auto_backfill_post_count: int | None = None,
        delivery_timeout_s: int | None = 60,
    ) -> None:
        self._stream_factory = stream_factory
        self._store = store
        self._my_call = my_call.upper()
        self._name = name
        self._on_event = on_event
        self._keepalive_interval = keepalive_interval
        self._keepalive_max_minutes = keepalive_max_minutes
        self._last_outbound_ts: float = time.monotonic()
        self._auto_reconnect = auto_reconnect
        self._reconnect_initial = reconnect_initial_backoff
        self._reconnect_max = reconnect_max_backoff
        if reconnect_max_retries < 0:
            raise ValueError(
                f"reconnect_max_retries must be >= 0 (got {reconnect_max_retries}); "
                "use 0 for unlimited retries"
            )
        self._reconnect_max_retries = reconnect_max_retries
        self._connect_script: list[HopStep] = list(connect_script or [])
        self._hop_progress = hop_progress
        self._auto_backfill_post_count = auto_backfill_post_count
        if delivery_timeout_s is not None and delivery_timeout_s <= 0:
            raise ValueError(
                f"delivery_timeout_s must be a positive int or None "
                f"(got {delivery_timeout_s!r})"
            )
        self._delivery_timeout_s = delivery_timeout_s
        # Outbound rows awaiting an ack. Each entry is keyed by the
        # row's natural identifier (msg_id for DMs, ts for posts since
        # outbound posts are ours and ts is unique at ms resolution) and
        # holds the asyncio.Task that will fire the timeout event if no
        # ack arrives in time. Cleared by mr/cpr handlers and on close.
        self._dm_timeout_tasks: dict[str, asyncio.Task] = {}
        self._post_timeout_tasks: dict[int, asyncio.Task] = {}
        # Outbound edits awaiting an ack. Same key shape as the original-
        # send dicts above, but separate slots so an unacked edit can
        # fire its own timeout independently of the original send's
        # delivered_ts (which would otherwise short-circuit the timer's
        # "still pending?" check). The value is the ``edts`` we sent;
        # the timer fires only if that exact edts is still pending when
        # the timer wakes — a newer edit supersedes an older pending one.
        self._dm_edit_timeout_tasks: dict[str, asyncio.Task] = {}
        self._post_edit_timeout_tasks: dict[int, asyncio.Task] = {}
        self._pending_dm_edits: dict[str, int] = {}
        self._pending_post_edits: dict[int, int] = {}
        self._first_frame_seen = False

        self._stream: AsyncByteStream | None = None
        self._online: set[str] = set()
        # Memoised callsign -> resolved display name (or None when no row
        # exists). Avoids a SQLite round trip per ``ham_name`` call from
        # the TUI's render hot paths (every ``MessageRow.refresh_label``,
        # every ``_refresh_online_pane`` entry). Invalidated per-key by
        # ``_on_he`` when the row is rewritten and cleared on each
        # ``_handshake`` so a fresh connect doesn't carry stale entries
        # across a state-dir change.
        self._ham_name_cache: dict[str, str | None] = {}
        # cid -> pending-post count from the most recent `pch` (server told us
        # the channel exceeds maxNewPostsToReturnPerChannelOnConnect; we need
        # to send `cu` to unpause and download).
        self._paused_channels: dict[int, int] = {}
        # cid -> awaiting future for the next `cs` (s=1) ack on that
        # channel. Used by ``subscribe_and_wait`` to surface the server's
        # ``pc`` count to a calling UI/test before any follow-up `cpb`.
        self._cs_ack_waiters: dict[int, asyncio.Future[int]] = {}
        self._decoder = codec.FrameDecoder()
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._closed = False
        self._handlers: dict[str, HandlerFn] = {}
        self._register_default_handlers()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the link and run the WPS handshake."""
        await self._handshake()
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._spawn_keepalive()

    async def _handshake(self) -> None:
        """Open a fresh stream, send callsign + type-`c`. Caller owns lifecycle."""
        self._decoder = codec.FrameDecoder()
        self._first_frame_seen = False
        # Fresh handshake → wait for the server's `o` payload to repopulate.
        self._online.clear()
        self._ham_name_cache.clear()
        # Paused-channel and cs-ack-waiter state is per-connection: the
        # server's `pch` / `cs` replies only refer to *this* session.
        self._paused_channels.clear()
        for fut in self._cs_ack_waiters.values():
            if not fut.done():
                fut.cancel()
        self._cs_ack_waiters.clear()
        # Reset the silence-guard clock so a long-idle reconnect doesn't
        # immediately re-trip it.
        self._last_outbound_ts = time.monotonic()
        self._stream = self._stream_factory()
        await self._stream.open()
        if self._connect_script:
            # Drive the node-prompt hop chain (e.g. ``C MB7NPW`` →
            # ``Connected`` → ``C WPS`` → …) before any WPS bytes flow.
            # Must complete fully before the FrameDecoder starts consuming.
            await run_connect_script(
                self._stream,
                self._connect_script,
                on_progress=self._hop_progress,
            )
        if not self._stream.injects_callsign:
            # Direct TCP to the WPS daemon: client must send the callsign
            # line itself. RHP paths skip this — the upstream node has
            # already pre-sent it on the WPS-facing socket.
            await self._stream.send(f"{self._my_call}\r\n".encode("utf-8"))
        record = self._store.connect_record(self._name, self._my_call, CLIENT_VERSION)
        # _send is not yet usable (connected event isn't set), so log
        # directly here to keep the trace symmetric with post-handshake
        # outbound traffic.
        logger.debug("WPS> %s", json.dumps(record, separators=(",", ":")))
        await self._stream.send(codec.encode(record))
        self._connected.set()

    async def close(self) -> None:
        self._closed = True
        self._connected.clear()
        # Pending timeout timers would otherwise outlive the client and
        # try to emit on a torn-down on_event; drop them up front.
        self._cancel_all_delivery_timers()
        # Pending subscribe waiters would block their callers forever
        # if quit fires mid-`subscribe_and_wait`; cancel them so the
        # caller's `await` raises CancelledError cleanly.
        for fut in self._cs_ack_waiters.values():
            if not fut.done():
                fut.cancel()
        self._cs_ack_waiters.clear()
        for task in (self._keepalive_task, self._reader_task, self._reconnect_task):
            if task is not None:
                task.cancel()
        for task in (self._keepalive_task, self._reader_task, self._reconnect_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._stream is not None:
            try:
                await self._stream.close()
            except Exception:
                pass
            self._stream = None

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """Block until the link is up. Useful after a reconnect kicked in."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._closed

    @property
    def is_auto_reconnect(self) -> bool:
        return self._auto_reconnect

    def online_users(self) -> list[str]:
        """Snapshot of the current online-user roster.

        Seeded by the type-``o`` payload the server sends during the
        connect sequence and kept in sync by ``uc`` (connect) / ``ud``
        (disconnect) events thereafter. Cleared at the start of each
        handshake so reconnects don't surface stale entries.
        """
        return sorted(self._online)

    def ham_name(self, call: str | None) -> str | None:
        """Display name for ``call`` from the local hams table, or ``None``.

        Names are populated by ``he`` (ham enquiry) responses — the wire
        protocol's ``uc`` / ``ud`` / ``o`` / ``m`` / ``cp`` events carry
        callsigns only, so UIs that want to show a name alongside a call
        have to look it up here.

        Memoised in ``_ham_name_cache``. ``None`` is a valid cached
        value (no row), so misses are detected via ``in`` rather than
        a default. Cache entries are evicted per-callsign by ``_on_he``
        and cleared wholesale on each ``_handshake``.
        """
        if not call:
            return None
        key = call.upper()
        cache = self._ham_name_cache
        if key in cache:
            return cache[key]
        row = self._store.lookup_ham(key)
        name = (row.get("name") if row else None) or None
        cache[key] = name
        return name

    def paused_channels(self) -> dict[int, int]:
        """Channels the server flagged via ``pch`` as having too many new
        posts to deliver automatically. Maps cid → pending-post count.

        Empty until a ``pch`` arrives. Each entry is cleared once the
        client sends a successful :meth:`unpause_channel` for that cid,
        and the whole map is cleared at the start of every handshake.
        """
        return dict(self._paused_channels)

    @property
    def delivery_timeout_s(self) -> int | None:
        return self._delivery_timeout_s

    def set_delivery_timeout_s(self, value: int | None) -> None:
        """Update the delivery-timeout used for *future* sends.

        In-flight timers were scheduled with whatever value was current
        at send time; they continue with that delay. The ``/set``
        slash-command pairs this with the same change on
        ``SessionOptions`` so the verbose-render path also picks it up.
        """
        if value is not None and value <= 0:
            raise ValueError(
                f"delivery_timeout_s must be a positive int or None "
                f"(got {value!r})"
            )
        self._delivery_timeout_s = value

    @property
    def auto_backfill_post_count(self) -> int | None:
        """Default post count offered by the ``/sub`` and ``/unpause``
        modal prompts. UIs read this to seed the historic-count Edit.
        Does NOT trigger automatic unpause on ``pch`` — the user has to
        confirm explicitly via the modal or ``/unpause``."""
        return self._auto_backfill_post_count

    # ------------------------------------------------------------------
    # Outgoing convenience
    # ------------------------------------------------------------------

    async def send_message(self, to_call: str, text: str, *, reply_id: str | None = None) -> str:
        # DM wire convention is seconds-since-epoch for `ts` (web client
        # sends `Math.round(Date.now()/1e3)`); `_id` keeps a ms prefix
        # because that's what the web client uses for it
        # (`${Math.round(Date.now())}-${call}`) and we want the same
        # opaque id-space the server already deduplicates against.
        now_ms = int(time.time() * 1000)
        ts = now_ms // 1000
        msg_id = f"{now_ms}-{self._my_call}"
        body = {
            "t": "m",
            "_id": msg_id,
            "fc": self._my_call,
            "tc": to_call.upper(),
            "m": text,
            "ts": ts,
            # ms=0 marks the row as "sent, not yet acked" so the verbose
            # render can distinguish it from rows that predate the
            # delivery-tracking columns (where msg_status is NULL).
            "ms": 0,
        }
        if reply_id:
            body["r"] = reply_id
        await self._send(body)
        # Persist immediately so a crash mid-flight still records what we tried.
        self._store.upsert_message(body)
        self._schedule_dm_timeout(msg_id)
        return msg_id

    async def post(self, channel_id: int, text: str, *, reply_ts: int | None = None,
                   reply_from: str | None = None,
                   at_calls: list[str] | None = None) -> int:
        ts = int(time.time() * 1000)
        body = {
            "t": "cp",
            "cid": channel_id,
            "fc": self._my_call,
            "ts": ts,
            "p": text,
        }
        if reply_ts:
            body["rts"] = reply_ts
        if reply_from:
            body["rfc"] = reply_from.upper()
        if at_calls:
            # Wire form is a JSON array of callsigns (web client:
            # ``Ae.at = [...b.map(le => le.c)]``). Uppercase to match
            # the server's identity rules and other outbound frames.
            body["at"] = [c.upper() for c in at_calls]
        await self._send(body)
        self._store.upsert_post(channel_id, body)
        self._schedule_post_timeout(channel_id, ts)
        return ts

    async def subscribe(self, channel_id: int, *, last_post: int = 0) -> None:
        await self._send({"t": "cs", "s": 1, "cid": channel_id, "lcp": last_post})

    async def subscribe_and_wait(
        self,
        channel_id: int,
        *,
        last_post: int = 0,
        timeout: float | None = None,
    ) -> int:
        """Send a `cs` subscribe and block until the server's ack arrives.

        Returns the ``pc`` field — the number of historic posts the server
        reports as available for that channel — so the caller can decide
        how many to follow up with via :meth:`request_post_batch`.

        No timeout by default — packet networks can be very slow and the
        web client doesn't bound this either. Tests pass an explicit
        ``timeout`` to keep themselves fast. The application-level silence
        guard tears the link down if it's truly stuck. Raises
        :class:`asyncio.TimeoutError` on timeout (when one is given),
        :class:`ConnectionError` if the link drops while waiting (the
        handshake reset cancels the waiter).
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        # Last subscribe wins if a stale waiter is already there (e.g. the
        # caller cancelled and retried) — cancel the old one.
        existing = self._cs_ack_waiters.get(channel_id)
        if existing is not None and not existing.done():
            existing.cancel()
        self._cs_ack_waiters[channel_id] = fut
        try:
            await self.subscribe(channel_id, last_post=last_post)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._cs_ack_waiters.pop(channel_id, None)

    async def unsubscribe(self, channel_id: int) -> None:
        await self._send({"t": "cs", "s": 0, "cid": channel_id, "lcp": 0})

    async def unpause_channel(
        self,
        channel_id: int,
        *,
        post_count: int | None = None,
        logged_ts: int | None = None,
    ) -> None:
        """Send ``cu`` to unpause a channel the server flagged via ``pch``.

        Pass exactly one of ``post_count`` (last-N posts) or ``logged_ts``
        (all posts since that ms-epoch timestamp). The server will reply
        with one or more ``cpb`` batches; the default handler persists
        them into the local store.

        Note: docs label this type ``uc``, but the server's dispatch
        actually keys on ``cu``. We mirror the code.
        """
        if (post_count is None) == (logged_ts is None):
            raise ValueError(
                "unpause_channel needs exactly one of post_count or logged_ts"
            )
        body: dict = {"t": "cu", "cid": channel_id}
        if post_count is not None:
            body["pc"] = int(post_count)
        else:
            body["lts"] = int(logged_ts)  # type: ignore[arg-type]
        await self._send(body)
        # Server clears its paused_channels record on receipt; track the
        # same locally so the UI doesn't keep prompting.
        self._paused_channels.pop(channel_id, None)

    async def request_post_batch(self, channel_id: int, post_count: int) -> None:
        """Send a client→server ``cpb`` to fetch the last ``post_count``
        posts for a (subscribed, non-paused) channel.

        Pair with :meth:`subscribe_and_wait` — the latter returns the
        server's ``pc`` count of available history, and the caller picks
        how many to actually download via this method. The server replies
        with one or more server-form ``cpb`` batches.
        """
        if post_count <= 0:
            raise ValueError(f"post_count must be positive, got {post_count!r}")
        await self._send({"t": "cpb", "cid": channel_id, "pc": int(post_count)})

    async def resend_message(self, msg_id: str) -> None:
        """Re-send a previously-sent DM identified by its server ``_id``.

        If the row has been edited (``edit_ts IS NOT NULL``), the latest
        edit is what the user actually wants on the wire — re-emit it as
        a ``med`` frame carrying the *current* body. Otherwise fall back
        to re-emitting the original ``m`` frame.

        Either form is idempotent on the server: ``m`` dedupes on
        ``_id``; ``med`` overwrites by ``_id``. The matching ``mr`` ack
        cancels the timeout (whichever variant) and refreshes
        ``delivered_ts``.

        Refuses rows that aren't ours — there's no protocol-level way to
        re-send someone else's message and the user has clearly typed
        the wrong lid.
        """
        row = self._store.lookup_message_by_id(msg_id)
        if row is None:
            raise ValueError(f"no local message with id {msg_id!r}")
        if (row.get("from_call") or "").upper() != self._my_call:
            raise ValueError("Cannot retry sending other users DMs")
        edts = row.get("edit_ts")
        if edts is not None:
            await self._send(
                {"t": "med", "_id": row["id"], "m": row["body"], "edts": int(edts)}
            )
            self._schedule_dm_edit_timeout(msg_id, int(edts))
            return
        body = {
            "t": "m",
            "_id": row["id"],
            "fc": row["from_call"],
            "tc": row["to_call"],
            "m": row["body"],
            "ts": row["ts"],
            "ms": 0,
        }
        if row.get("reply_id"):
            body["r"] = row["reply_id"]
        await self._send(body)
        self._schedule_dm_timeout(msg_id)

    async def resend_post(self, channel_id: int, ts: int) -> None:
        """Re-send a previously-sent channel post identified by ``(cid, ts)``.

        Edit-aware: if the post has an ``edit_ts``, re-emit ``cped``
        carrying the current body; otherwise re-emit the original ``cp``.
        Either form is server-idempotent and re-acked via ``cpr``.
        """
        row = self._store.lookup_post(channel_id, ts)
        if row is None:
            raise ValueError(
                f"no local post with cid={channel_id}, ts={ts}"
            )
        if (row.get("from_call") or "").upper() != self._my_call:
            raise ValueError("Cannot retry sending other users posts")
        edts = row.get("edit_ts")
        if edts is not None:
            await self._send(
                {
                    "t": "cped",
                    "cid": int(channel_id),
                    "ts": int(ts),
                    "p": row["body"],
                    "edts": int(edts),
                }
            )
            self._schedule_post_edit_timeout(int(ts), int(edts))
            return
        body = {
            "t": "cp",
            "cid": int(channel_id),
            "fc": row["from_call"],
            "ts": int(ts),
            "p": row["body"],
        }
        if row.get("reply_ts"):
            body["rts"] = row["reply_ts"]
        if row.get("reply_from"):
            body["rfc"] = row["reply_from"]
        # Mention list is JSON-encoded in the row; decode + restore.
        # `cped` resend skips this — the web client's edit frame
        # doesn't carry `at` (mentions are immutable across edits).
        at_raw = row.get("at_calls")
        if at_raw:
            try:
                import json as _json

                decoded = _json.loads(at_raw) if isinstance(at_raw, str) else at_raw
                if isinstance(decoded, list) and decoded:
                    body["at"] = [str(c).upper() for c in decoded]
            except (TypeError, ValueError):
                pass
        await self._send(body)
        self._schedule_post_timeout(int(channel_id), int(ts))

    async def edit_message(self, msg_id: str, new_text: str) -> None:
        """Edit a previously-sent DM. Updates the local row immediately
        (the server doesn't echo the edit back to the sender — we'd
        otherwise never see our own edit) and schedules an edit-specific
        delivery timeout that fires if the server's ``mr`` ack doesn't
        arrive in time.

        Refuses rows whose ``from_call`` isn't ours — the server enforces
        this too but the UI gets immediate feedback this way.
        """
        row = self._store.lookup_message_by_id(msg_id)
        if row is None:
            raise ValueError(f"no local message with id {msg_id!r}")
        if (row.get("from_call") or "").upper() != self._my_call:
            raise ValueError("Cannot edit other users DMs")
        edts = int(time.time() * 1000)
        await self._send({"t": "med", "_id": msg_id, "m": new_text, "edts": edts})
        self._store.apply_message_edit(msg_id, new_text, edts)
        self._store.bump_meta("last_edit", edts)
        self._schedule_dm_edit_timeout(msg_id, edts)

    async def edit_post(self, channel_id: int, ts: int, new_text: str) -> None:
        """Edit a previously-sent channel post. Same local-write +
        edit-timeout semantics as :meth:`edit_message`.

        Refuses rows whose ``from_call`` isn't ours.
        """
        row = self._store.lookup_post(int(channel_id), int(ts))
        if row is None:
            raise ValueError(
                f"no local post with cid={int(channel_id)}, ts={int(ts)}"
            )
        if (row.get("from_call") or "").upper() != self._my_call:
            raise ValueError("Cannot edit other users posts")
        edts = int(time.time() * 1000)
        await self._send(
            {
                "t": "cped",
                "cid": int(channel_id),
                "ts": int(ts),
                "p": new_text,
                "edts": edts,
            }
        )
        self._store.apply_post_edit(int(channel_id), int(ts), new_text, edts)
        self._store.bump_channel_last_edit(int(channel_id), edts)
        self._schedule_post_edit_timeout(int(ts), edts)

    async def react_message(self, msg_id: str, emoji: str, *, add: bool = True) -> None:
        # Normalise to the protocol's hex-codepoint form (e.g. literal
        # `👍` from the picker grid → `"1f44d"`). The wire and the
        # local store both hold the hex form, matching what inbound
        # `mem` / `memb` carry — so a peer's reaction we just received
        # and our own freshly-sent reaction render identically.
        emoji = emoji_to_wire(emoji)
        ets = int(time.time())
        await self._send(
            {"t": "mem", "a": 1 if add else 0, "_id": msg_id, "e": emoji, "ets": ets}
        )
        # WPS doesn't echo DM reactions back to the sender (see
        # `message_emoji_handler` in `wps.py` — it relays to
        # `message_to_update['fc']`, the message author). Write the row
        # locally so the UI shows the reaction immediately.
        if add:
            self._store.upsert_message_emoji(msg_id, emoji, self._my_call, ets)
        else:
            self._store.remove_message_emoji(msg_id, emoji)
            self._store.bump_meta("last_emoji", ets)

    async def react_post(self, channel_id: int, ts: int, emoji: str, *, add: bool = True) -> None:
        emoji = emoji_to_wire(emoji)
        ets = int(time.time())
        await self._send(
            {
                "t": "cpem",
                "a": 1 if add else 0,
                "ts": ts,
                "cid": channel_id,
                "ets": ets,
                "e": emoji,
            }
        )
        # `post_emoji_handler` in `wps.py` skips the sender too — the
        # reaction is broadcast to other subscribers but not back to us.
        if add:
            self._store.upsert_post_emoji(
                int(channel_id), int(ts), emoji, self._my_call, ets
            )
        else:
            self._store.remove_post_emoji(
                int(channel_id), int(ts), emoji, self._my_call
            )

    async def keep_alive(self) -> None:
        # _silence_reset=False: keepalives don't push the silence-guard
        # clock forward, mirroring the web client (only `Se` calls
        # `ne("RESET")`; the keepalive tick sends directly).
        await self._send({"t": "k"}, _silence_reset=False)

    # ------------------------------------------------------------------
    # Per-row delivery-timeout tracking
    # ------------------------------------------------------------------

    def _schedule_dm_timeout(self, msg_id: str) -> None:
        delay = self._delivery_timeout_s
        if delay is None:
            return
        # A resend / quick re-issue may land before the previous timer
        # has fired or been cancelled — replace it so we don't end up
        # with two firings for the same row.
        existing = self._dm_timeout_tasks.pop(msg_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        self._dm_timeout_tasks[msg_id] = asyncio.create_task(
            self._dm_timeout_loop(msg_id, float(delay))
        )

    def _cancel_dm_timeout(self, msg_id: str) -> None:
        task = self._dm_timeout_tasks.pop(msg_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_post_timeout(self, cid: int, ts: int) -> None:
        delay = self._delivery_timeout_s
        if delay is None:
            return
        existing = self._post_timeout_tasks.pop(ts, None)
        if existing is not None and not existing.done():
            existing.cancel()
        self._post_timeout_tasks[ts] = asyncio.create_task(
            self._post_timeout_loop(int(cid), int(ts), float(delay))
        )

    def _cancel_post_timeout(self, ts: int) -> None:
        task = self._post_timeout_tasks.pop(ts, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_dm_edit_timeout(self, msg_id: str, edts: int) -> None:
        """Arm an edit-specific timer for an outbound ``med`` frame.

        Tracked separately from the original-send timer so the timer's
        "still pending?" check can use a per-edit token (``edts``)
        instead of ``delivered_ts`` — which is set by the *original*
        send's ``mr`` and would short-circuit the check otherwise.
        Storage in :attr:`_pending_dm_edits` is what drives that token
        comparison.
        """
        delay = self._delivery_timeout_s
        if delay is None:
            return
        self._pending_dm_edits[msg_id] = int(edts)
        existing = self._dm_edit_timeout_tasks.pop(msg_id, None)
        if existing is not None and not existing.done():
            existing.cancel()
        self._dm_edit_timeout_tasks[msg_id] = asyncio.create_task(
            self._dm_edit_timeout_loop(msg_id, int(edts), float(delay))
        )

    def _cancel_dm_edit_timeout(self, msg_id: str) -> None:
        self._pending_dm_edits.pop(msg_id, None)
        task = self._dm_edit_timeout_tasks.pop(msg_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_post_edit_timeout(self, ts: int, edts: int) -> None:
        delay = self._delivery_timeout_s
        if delay is None:
            return
        self._pending_post_edits[int(ts)] = int(edts)
        existing = self._post_edit_timeout_tasks.pop(int(ts), None)
        if existing is not None and not existing.done():
            existing.cancel()
        self._post_edit_timeout_tasks[int(ts)] = asyncio.create_task(
            self._post_edit_timeout_loop(int(ts), int(edts), float(delay))
        )

    def _cancel_post_edit_timeout(self, ts: int) -> None:
        self._pending_post_edits.pop(int(ts), None)
        task = self._post_edit_timeout_tasks.pop(int(ts), None)
        if task is not None and not task.done():
            task.cancel()

    async def _dm_timeout_loop(self, msg_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._dm_timeout_tasks.pop(msg_id, None)
            raise
        try:
            if self._closed:
                return
            row = self._store.lookup_message_by_id(msg_id)
            # Row could have been wiped (state-dir reset) or the ack
            # could have raced our cancellation; either way the
            # delivered_ts column is the source of truth.
            if row is None or row.get("delivered_ts") is not None:
                return
            await self._emit_event(
                {
                    "t": "_delivery_timeout",
                    "kind": "dm",
                    "msg_id": msg_id,
                    "lid": row.get("lid"),
                    "peer": row.get("to_call"),
                    "ts": row.get("ts"),
                }
            )
        finally:
            self._dm_timeout_tasks.pop(msg_id, None)

    async def _post_timeout_loop(self, cid: int, ts: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._post_timeout_tasks.pop(ts, None)
            raise
        try:
            if self._closed:
                return
            row = self._store.lookup_post(cid, ts)
            if row is None or row.get("delivered_ts") is not None:
                return
            await self._emit_event(
                {
                    "t": "_delivery_timeout",
                    "kind": "post",
                    "cid": cid,
                    "lid": row.get("lid"),
                    "ts": ts,
                }
            )
        finally:
            self._post_timeout_tasks.pop(ts, None)

    async def _dm_edit_timeout_loop(
        self, msg_id: str, edts: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._dm_edit_timeout_tasks.pop(msg_id, None)
            raise
        try:
            if self._closed:
                return
            # The pending-edits map is the source of truth: cleared by
            # the matching `mr` ack, overwritten by a newer edit (so an
            # older timer firing late finds the entry mismatched and
            # silently no-ops).
            if self._pending_dm_edits.get(msg_id) != edts:
                return
            row = self._store.lookup_message_by_id(msg_id)
            if row is None:
                return
            await self._emit_event(
                {
                    "t": "_delivery_timeout",
                    "kind": "dm",
                    "is_edit": True,
                    "msg_id": msg_id,
                    "lid": row.get("lid"),
                    "peer": row.get("to_call"),
                    "ts": row.get("ts"),
                    "edit_ts": edts,
                }
            )
        finally:
            self._dm_edit_timeout_tasks.pop(msg_id, None)

    async def _post_edit_timeout_loop(
        self, ts: int, edts: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._post_edit_timeout_tasks.pop(ts, None)
            raise
        try:
            if self._closed:
                return
            if self._pending_post_edits.get(ts) != edts:
                return
            row = self._store.lookup_post_by_from_ts(self._my_call, ts)
            if row is None:
                return
            cid = row.get("channel_id")
            await self._emit_event(
                {
                    "t": "_delivery_timeout",
                    "kind": "post",
                    "is_edit": True,
                    "cid": cid,
                    "lid": row.get("lid"),
                    "ts": ts,
                    "edit_ts": edts,
                }
            )
        finally:
            self._post_edit_timeout_tasks.pop(ts, None)

    async def _emit_event(self, obj: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(obj)
        except Exception:
            pass

    def _cancel_all_delivery_timers(self) -> None:
        for task in list(self._dm_timeout_tasks.values()):
            if not task.done():
                task.cancel()
        self._dm_timeout_tasks.clear()
        for task in list(self._post_timeout_tasks.values()):
            if not task.done():
                task.cancel()
        self._post_timeout_tasks.clear()
        for task in list(self._dm_edit_timeout_tasks.values()):
            if not task.done():
                task.cancel()
        self._dm_edit_timeout_tasks.clear()
        for task in list(self._post_edit_timeout_tasks.values()):
            if not task.done():
                task.cancel()
        self._post_edit_timeout_tasks.clear()
        self._pending_dm_edits.clear()
        self._pending_post_edits.clear()

    async def _send(self, obj: dict, *, _silence_reset: bool = True) -> None:
        """Single point of egress; serialised, raises if disconnected.

        Takes the wire dict (not bytes) so the application-protocol layer
        can be traced at DEBUG before compression — wire-level dumps
        (websockets, RHP) only show the framed/compressed bytes, which
        are unreadable for any frame that crosses the compression
        threshold.
        """
        logger.debug("WPS> %s", json.dumps(obj, separators=(",", ":")))
        data = codec.encode(obj)
        async with self._send_lock:
            if self._stream is None or not self._connected.is_set():
                raise ConnectionError("not connected")
            await self._stream.send(data)
            if _silence_reset:
                self._last_outbound_ts = time.monotonic()

    # ------------------------------------------------------------------
    # Reader / dispatch
    # ------------------------------------------------------------------

    def on(self, type_code: str, fn: HandlerFn) -> None:
        """Register a handler for one WPS message type."""
        self._handlers[type_code] = fn

    async def _reader_loop(self) -> None:
        try:
            while not self._closed:
                assert self._stream is not None
                chunk = await self._stream.recv()
                if not chunk:
                    await self._handle_link_loss(reason="eof")
                    return
                try:
                    objs = list(self._decoder.feed(chunk))
                except codec.FrameDecodeError as exc:
                    raise self._wrap_decode_error(exc) from exc
                for obj in objs:
                    self._first_frame_seen = True
                    logger.debug("WPS< %s", json.dumps(obj, separators=(",", ":")))
                    await self._dispatch(obj)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._on_event:
                await self._on_event({"t": "_error", "exc": repr(exc)})
            await self._handle_link_loss(reason=repr(exc))

    def _wrap_decode_error(self, exc: codec.FrameDecodeError) -> Exception:
        """Pre-handshake decode failures almost always mean the connect_script
        didn't reach WPS — surface a one-line hint instead of bubbling the
        raw JSONDecodeError, which doesn't tell the user anything useful."""
        snippet = exc.payload[:80].decode("latin-1", errors="replace").strip()
        if not self._first_frame_seen:
            return RuntimeError(
                f"connect_sequence likely incomplete: server's first reply "
                f"isn't a WPS frame, it's plain text — {snippet!r}. Check "
                f"that every hop in the script matches the node's prompts."
            )
        return RuntimeError(str(exc))

    async def _handle_link_loss(self, *, reason: str) -> None:
        if self._closed:
            return
        self._connected.clear()
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._stream is not None:
            try:
                await self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._on_event:
            await self._on_event({"t": "_disconnect", "reason": reason})
        if self._auto_reconnect and not self._closed and self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = self._reconnect_initial
        attempt = 0
        cap = self._reconnect_max_retries  # 0 = unlimited
        try:
            while not self._closed:
                if cap and attempt >= cap:
                    if self._on_event:
                        await self._on_event(
                            {"t": "_reconnect_giveup", "attempts": attempt}
                        )
                    return
                attempt += 1
                if self._on_event:
                    await self._on_event(
                        {"t": "_reconnecting", "attempt": attempt, "delay": delay}
                    )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                if self._closed:
                    return
                try:
                    await self._handshake()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._on_event:
                        await self._on_event(
                            {"t": "_reconnect_failed", "attempt": attempt, "exc": repr(exc)}
                        )
                    delay = min(delay * 2, self._reconnect_max)
                    continue
                self._reader_task = asyncio.create_task(self._reader_loop())
                self._spawn_keepalive()
                if self._on_event:
                    await self._on_event({"t": "_reconnected", "attempt": attempt})
                return
        finally:
            self._reconnect_task = None

    def _spawn_keepalive(self) -> None:
        if self._keepalive_interval is None:
            return
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        assert self._keepalive_interval is not None
        max_silence_s = (
            self._keepalive_max_minutes * 60.0
            if self._keepalive_max_minutes is not None
            else None
        )
        try:
            while not self._closed and self._connected.is_set():
                await asyncio.sleep(self._keepalive_interval)
                if not self._connected.is_set() or self._closed:
                    return
                if max_silence_s is not None:
                    silent_for = time.monotonic() - self._last_outbound_ts
                    if silent_for >= max_silence_s:
                        await self._silence_disconnect(silent_for)
                        return
                try:
                    await self.keep_alive()
                except ConnectionError:
                    return
                except Exception:
                    return
        except asyncio.CancelledError:
            raise

    async def _silence_disconnect(self, silent_for: float) -> None:
        """Mirror the web client's ``timeoutTriggeredDisconnect``: close
        cleanly and don't auto-reconnect — the user has been idle long
        enough that we're hanging up on them rather than spinning."""
        minutes = silent_for / 60.0
        if self._on_event:
            try:
                await self._on_event(
                    {"t": "_silence_disconnect", "minutes": round(minutes, 1)}
                )
            except Exception:
                pass
        # close() flips _closed=True before tearing down the reader/
        # keepalive tasks, so the auto-reconnect path won't fire.
        asyncio.create_task(self.close())

    async def _dispatch(self, obj: dict) -> None:
        # Route to a registered handler (which may ignore unknown types).
        t = obj.get("t")
        handler = self._handlers.get(t)
        if handler is not None:
            await handler(obj)
        if self._on_event:
            await self._on_event(obj)

    def _register_default_handlers(self) -> None:
        """Default handlers persist incoming data into the local store."""

        def _now_ms() -> int:
            return int(time.time() * 1000)

        def _ts_to_ms(ts: object) -> int | None:
            # DMs use seconds on the wire, posts use ms; the magnitude
            # disambiguates (>= 1e12 is ms, since 1e12 ms ≈ year 2001).
            if not isinstance(ts, (int, float)):
                return None
            n = int(ts)
            return n * 1000 if n < 1_000_000_000_000 else n

        def _self_delivered_ts(o: dict) -> int | None:
            # Synthetic delivered_ts to seed for inbound rows whose
            # `fc` is our own callsign. Anything we receive from the
            # server is by definition on the server, so the row should
            # not render as "still pending an ack" — even if this client
            # didn't originate the send (different client instance,
            # cleared state-dir mid-send, etc.). Prefer the row's own
            # `ts` so verbose render shows a sane "Delivered in 0s"
            # rather than a duration measured against the local clock.
            return _ts_to_ms(o.get("ts")) or _now_ms()

        async def _on_message(o: dict) -> None:
            # Inbound real-time DM. Skip receipt metadata if the row is
            # actually our own message echoed back — in that case the
            # send-side upsert already wrote the canonical row and we
            # don't want to retroactively label it "received realtime".
            from_call = (o.get("fc") or "").upper()
            if from_call and from_call != self._my_call:
                self._store.upsert_message(o, realtime=True, received_ts=_now_ms())
            else:
                self._store.upsert_message(o, delivered_ts=_self_delivered_ts(o))

        async def _on_message_batch(o: dict) -> None:
            now = _now_ms()
            for m in o.get("m", []):
                fc = (m.get("fc") or "").upper()
                if fc and fc != self._my_call:
                    self._store.upsert_message(m, realtime=False, received_ts=now)
                else:
                    self._store.upsert_message(m, delivered_ts=_self_delivered_ts(m))

        async def _on_med(o: dict) -> None:
            """Real-time DM edit. Server forwards `med` to the recipient
            only — the sender already updated locally on send."""
            msg_id = o.get("_id")
            new_body = o.get("m")
            edts = o.get("edts")
            if not isinstance(msg_id, str) or new_body is None:
                return
            # `med` may omit `edts` (the server's real-time send path
            # doesn't include it; only the medb batch does). Fall back
            # to the local clock so the row still gets a sensible
            # edit-time stamp for verbose render.
            if not isinstance(edts, int):
                edts = _now_ms()
            self._store.apply_message_edit(msg_id, new_body, edts)
            self._store.bump_meta("last_edit", edts)

        async def _on_message_edit(o: dict) -> None:
            """Connect-batch DM edits. Wire shape is
            ``{t:medb, med:[{_id, m, edts}, ...]}`` — note the array key
            is ``med``, not ``m``, despite what one of the JSON examples
            in the spec doc suggests. The server source and the web
            client both use ``med`` (see ``wps.py`` and the reference
            web client's batch handler)."""
            highest = 0
            for m in o.get("med", []):
                msg_id = m.get("_id")
                new_body = m.get("m")
                edts = m.get("edts")
                if (
                    not isinstance(msg_id, str)
                    or new_body is None
                    or not isinstance(edts, int)
                ):
                    continue
                self._store.apply_message_edit(msg_id, new_body, edts)
                if edts > highest:
                    highest = edts
            if highest:
                self._store.bump_meta("last_edit", highest)

        async def _on_cped(o: dict) -> None:
            """Real-time channel post edit. Forwarded to other
            subscribers (not the sender)."""
            cid = o.get("cid")
            ts = o.get("ts")
            new_body = o.get("p")
            edts = o.get("edts")
            if (
                not isinstance(cid, int)
                or not isinstance(ts, int)
                or new_body is None
            ):
                return
            if not isinstance(edts, int):
                edts = _now_ms()
            self._store.apply_post_edit(cid, ts, new_body, edts)
            self._store.bump_channel_last_edit(cid, edts)

        async def _on_cpedb(o: dict) -> None:
            """Connect-batch post edits. Wire shape is
            ``{t:cpedb, ed:[{cid, ts, p, edts}, ...]}`` — array key is
            ``ed`` per the server source (``wps.py``) and the web
            client's batch handler."""
            for entry in o.get("ed", []):
                cid = entry.get("cid")
                ts = entry.get("ts")
                new_body = entry.get("p")
                edts = entry.get("edts")
                if (
                    not isinstance(cid, int)
                    or not isinstance(ts, int)
                    or new_body is None
                    or not isinstance(edts, int)
                ):
                    continue
                self._store.apply_post_edit(cid, ts, new_body, edts)
                self._store.bump_channel_last_edit(cid, edts)

        async def _on_post(o: dict) -> None:
            from_call = (o.get("fc") or "").upper()
            if from_call and from_call != self._my_call:
                self._store.upsert_post(
                    o["cid"], o, realtime=True, received_ts=_now_ms()
                )
            else:
                self._store.upsert_post(
                    o["cid"], o, delivered_ts=_self_delivered_ts(o)
                )

        async def _on_post_batch(o: dict) -> None:
            cid = o.get("cid")
            if cid is None:
                return
            now = _now_ms()
            for p in o.get("p", []):
                fc = (p.get("fc") or "").upper()
                if fc and fc != self._my_call:
                    self._store.upsert_post(
                        cid, p, realtime=False, received_ts=now
                    )
                else:
                    self._store.upsert_post(
                        cid, p, delivered_ts=_self_delivered_ts(p)
                    )
                # `cpb` posts carry their current per-post reaction
                # state inline as `e: [{e, c[]}, ...]` with an `ets`
                # cursor — same shape as a `cpemb` group entry. The
                # connect handler emits a separate `cpemb` for posts
                # in already-subscribed channels (`wps/wps.py`'s
                # `channels_connect_handler`), but a mid-session `cs`
                # + `cpb` flow does not — so without applying the
                # embedded state here, reactions on historic posts
                # only appear after the next reconnect.
                ts = p.get("ts")
                ets = p.get("ets")
                e = p.get("e")
                if isinstance(ts, int) and isinstance(ets, int):
                    self._store.apply_post_emoji_batch(
                        int(cid), int(ts), e if isinstance(e, list) else [], int(ets)
                    )

        async def _on_mem(o: dict) -> None:
            """Real-time DM emoji update. Wire form is the *full* current
            emoji list for the message (see CHANNELS.md / MESSAGES.md).
            We replace local state from the list, attributing any new
            emoji to the DM peer; existing rows we wrote ourselves keep
            their callsign so our own reactions stay attributed to us.
            """
            msg_id = o.get("_id")
            ets = o.get("ets")
            e = o.get("e")
            if not isinstance(msg_id, str) or not isinstance(ets, int):
                return
            emojis: list[str] = []
            if isinstance(e, list):
                emojis = [s for s in e if isinstance(s, str)]
            elif isinstance(e, str):
                # Real-time relay reuses the outbound shape (single
                # `e`); apply it as a single-item replacement so peer
                # adds are attributed correctly.
                emojis = [e]
            row = self._store.lookup_message_by_id(msg_id)
            if row is None:
                return
            from_call = (row.get("from_call") or "").upper()
            to_call = (row.get("to_call") or "").upper()
            peer = to_call if from_call == self._my_call else from_call
            self._store.apply_message_emoji_list(msg_id, peer, emojis, ets)

        async def _on_memb(o: dict) -> None:
            """Connect-batch DM emoji updates. Wire shape is
            ``{t:memb, mem:[{_id, e:[...], ets}, ...]}`` per the server
            (`wps.py` builds it as `{"t": "memb", "mem": [...]}`).
            """
            highest = 0
            for entry in o.get("mem", []):
                msg_id = entry.get("_id")
                ets = entry.get("ets")
                e = entry.get("e") or []
                if not isinstance(msg_id, str) or not isinstance(ets, int):
                    continue
                emojis = [s for s in e if isinstance(s, str)]
                row = self._store.lookup_message_by_id(msg_id)
                if row is None:
                    continue
                from_call = (row.get("from_call") or "").upper()
                to_call = (row.get("to_call") or "").upper()
                peer = to_call if from_call == self._my_call else from_call
                self._store.apply_message_emoji_list(msg_id, peer, emojis, ets)
                if ets > highest:
                    highest = ets
            if highest:
                self._store.bump_meta("last_emoji", highest)

        async def _on_cpem(o: dict) -> None:
            """Real-time channel post emoji update. The server adds
            ``fc`` (reactor callsign) before relaying — see
            ``post_emoji_handler`` in ``wps.py``."""
            cid = o.get("cid")
            ts = o.get("ts")
            ets = o.get("ets")
            emoji = o.get("e")
            action = o.get("a")
            fc = (o.get("fc") or "").upper()
            if (
                not isinstance(cid, int)
                or not isinstance(ts, int)
                or not isinstance(ets, int)
                or not isinstance(emoji, str)
                or not fc
            ):
                return
            if action == 1:
                self._store.upsert_post_emoji(cid, ts, emoji, fc, ets)
            elif action == 0:
                self._store.remove_post_emoji(cid, ts, emoji, fc)

        async def _on_cpemb(o: dict) -> None:
            """Connect-batch post emojis. Wire shape:
            ``{t:cpemb, e:[{cid, ts, ets, e:[{e,c[]}, ...]}, ...]}``."""
            for group in o.get("e", []):
                cid = group.get("cid")
                ts = group.get("ts")
                ets = group.get("ets")
                entries = group.get("e") or []
                if (
                    not isinstance(cid, int)
                    or not isinstance(ts, int)
                    or not isinstance(ets, int)
                ):
                    continue
                self._store.apply_post_emoji_batch(cid, ts, entries, ets)

        async def _on_message_response(o: dict) -> None:
            # `mr` ack for an outbound DM. Only carries `_id`; record the
            # delivery against the local clock since the wire frame has
            # no dts. `mr` is shared by the original-send path and the
            # edit path (server acks both with the same frame), so cancel
            # whichever timer slot is currently armed for this msg_id.
            msg_id = o.get("_id")
            if isinstance(msg_id, str):
                self._store.mark_message_delivered(msg_id, _now_ms())
                self._cancel_dm_timeout(msg_id)
                self._cancel_dm_edit_timeout(msg_id)

        async def _on_post_response(o: dict) -> None:
            # `cpr` ack for an outbound channel post. Carries `ts` (the
            # post's timestamp) and `dts` (server's delivery timestamp).
            # Same dual-purpose ack as `mr` — covers both the original
            # send and any subsequent edit, so we cancel both slots.
            ts = o.get("ts")
            dts = o.get("dts")
            if isinstance(ts, int):
                self._store.mark_post_delivered(
                    from_call=self._my_call,
                    ts=ts,
                    delivered_ts=int(dts) if isinstance(dts, int) else _now_ms(),
                )
                self._cancel_post_timeout(int(ts))
                self._cancel_post_edit_timeout(int(ts))

        async def _on_subscribe_ack(o: dict) -> None:
            cid = o["cid"]
            subscribed = bool(o.get("s", 0))
            self._store.set_subscription(cid, subscribed)
            # Resolve any caller waiting on `subscribe_and_wait` for this
            # cid. `pc` only appears on s=1 acks and only when the
            # channel has historic posts; default to 0 otherwise so the
            # caller gets a clean numeric answer either way.
            if subscribed:
                fut = self._cs_ack_waiters.get(cid)
                if fut is not None and not fut.done():
                    pc = o.get("pc")
                    fut.set_result(pc if isinstance(pc, int) else 0)

        async def _on_paused_headers(o: dict) -> None:
            for ch in o.get("ch", []):
                cid = ch.get("cid")
                pt = ch.get("pt")
                if not isinstance(cid, int) or not isinstance(pt, int):
                    continue
                self._paused_channels[cid] = pt

        async def _on_he(o: dict) -> None:
            for h in o.get("h", []):
                if isinstance(h, dict):
                    call = h.get("c", "")
                    self._store.upsert_ham(call, h.get("n", ""), h.get("ts", 0))
                    if call:
                        self._ham_name_cache.pop(call.upper(), None)

        async def _on_online(o: dict) -> None:
            self._online = {c.upper() for c in o.get("o", []) if c}

        async def _on_user_connect(o: dict) -> None:
            call = (o.get("c") or "").upper()
            if call:
                self._online.add(call)

        async def _on_user_disconnect(o: dict) -> None:
            call = (o.get("c") or "").upper()
            self._online.discard(call)

        self._handlers["m"] = _on_message
        self._handlers["mb"] = _on_message_batch
        self._handlers["med"] = _on_med
        self._handlers["medb"] = _on_message_edit
        self._handlers["mem"] = _on_mem
        self._handlers["memb"] = _on_memb
        self._handlers["mr"] = _on_message_response
        self._handlers["cp"] = _on_post
        self._handlers["cpb"] = _on_post_batch
        self._handlers["cped"] = _on_cped
        self._handlers["cpedb"] = _on_cpedb
        self._handlers["cpem"] = _on_cpem
        self._handlers["cpemb"] = _on_cpemb
        self._handlers["cpr"] = _on_post_response
        self._handlers["cs"] = _on_subscribe_ack
        self._handlers["pch"] = _on_paused_headers
        self._handlers["he"] = _on_he
        self._handlers["o"] = _on_online
        self._handlers["uc"] = _on_user_connect
        self._handlers["ud"] = _on_user_disconnect
