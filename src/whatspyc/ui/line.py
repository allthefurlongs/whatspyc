"""prompt_toolkit-based interactive line UI.

Renders incoming WPS messages above the prompt without garbling the input
line, via ``patch_stdout``. Slash-command parsing routes through the
provided ``WpsClient``.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import time
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from whatspyc.config import ChannelInfo
from whatspyc.ui import help as help_data
from whatspyc.ui import reply_prefix_text, resolve_reply_meta, ts_to_ms
from whatspyc.ui.options import SessionOptions
from whatspyc.wps.client import WpsClient


class LineUI:
    def __init__(
        self,
        client: WpsClient,
        *,
        my_call: str,
        channels: list[ChannelInfo] | None = None,
        history_backfill: int = 3,
        options: SessionOptions | None = None,
        offline: bool = False,
    ) -> None:
        self._client = client
        self._my_call = my_call
        self._channels = list(channels or [])
        self._history_backfill = max(0, int(history_backfill))
        self._options = options or SessionOptions()
        self._offline = offline
        self._target: tuple[str, str] | None = None  # ("dm", call) or ("ch", str(cid))
        self._session: PromptSession = PromptSession()
        self._stop = asyncio.Event()
        # Set to "terminal" when the link drops with no auto-reconnect or
        # after auto-reconnect gives up; the cli reads this after run()
        # returns to decide whether to offer a reconnect/quit prompt.
        self.exit_reason: str | None = None

    def render_event(self, obj: dict) -> None:
        """Print an incoming WPS message in a human-friendly way.

        Called with stdout already patched (see ``run``)."""
        t = obj.get("t")
        if t == "m":
            self._render_dm(obj)
            self._maybe_bell()
        elif t == "mb":
            for m in obj.get("m", []):
                self._render_dm(m)
        elif t == "cp":
            self._render_post(obj.get("cid"), obj)
            self._maybe_bell()
        elif t == "cpb":
            cid = obj.get("cid")
            for p in obj.get("p", []):
                self._render_post(cid, p)
        elif t == "mr":
            if self._options.show_acks:
                print(self._fmt_mr_ack(obj))
        elif t == "cpr":
            if self._options.show_acks:
                print(self._fmt_cpr_ack(obj))
        elif t == "med":
            if self._options.show_edits:
                line = self._fmt_dm_edit(obj)
                if line is not None:
                    print(line)
        elif t == "cped":
            if self._options.show_edits:
                line = self._fmt_post_edit(obj)
                if line is not None:
                    print(line)
        elif t == "cs":
            cid = obj.get("cid")
            state = "subscribed" if obj.get("s") else "unsubscribed"
            pc = obj.get("pc")
            ref = self._channel_ref(int(cid)) if cid is not None else "ch"
            if obj.get("s") and isinstance(pc, int) and pc > 0:
                print(f"[{ref}] {state} ({pc} historic posts on server)")
            else:
                print(f"[{ref}] {state}")
        elif t == "uc":
            print(f"[user] {self._fmt_user(obj.get('c'))} connected")
        elif t == "ud":
            print(f"[user] {self._fmt_user(obj.get('c'))} disconnected")
        elif t == "o":
            print(f"[online] {', '.join(self._fmt_user(c) for c in obj.get('o', []))}")
        elif t == "pch":
            for ch in obj.get("ch", []):
                cid = ch.get("cid")
                ref = self._channel_ref(int(cid)) if cid is not None else "ch"
                print(
                    f"[paused {ref}] {ch.get('pt')} pending posts "
                    f"— /unpause {cid} [N] to download"
                )
        elif t == "_disconnect":
            reason = obj.get("reason")
            print(f"[link] disconnected{f' ({reason})' if reason else ''}")
            if not self._client.is_auto_reconnect:
                self._signal_terminal_link_loss()
        elif t == "_reconnecting":
            print(
                f"[link] reconnect attempt {obj.get('attempt')} in {obj.get('delay'):.1f}s"
            )
        elif t == "_reconnect_failed":
            print(f"[link] reconnect attempt {obj.get('attempt')} failed: {obj.get('exc')}")
        elif t == "_reconnected":
            print(f"[link] reconnected (attempt {obj.get('attempt')})")
        elif t == "_reconnect_giveup":
            print(
                f"[link] giving up after {obj.get('attempts')} reconnect attempts"
            )
            self._signal_terminal_link_loss()
        elif t == "_error":
            print(f"[error] {obj.get('exc')}")
        elif t == "_delivery_timeout":
            # Always print, regardless of show_acks — the user explicitly
            # asked for a real-time hint when an outbound row's ack
            # didn't arrive in time, since "no ack" is harder to notice
            # than "ack received".
            print(self._fmt_delivery_timeout(obj))
        # other types: ignore in line UI

    def _maybe_bell(self) -> None:
        """Ring the terminal bell when ``bell_on_activity`` is on.

        prompt_toolkit's ``patch_stdout`` will route the BEL byte
        through its renderer; the byte is non-printing so it doesn't
        disturb the visible buffer — the terminal emulator translates
        it to whatever (audible / visual / nothing) the user has
        configured.
        """
        if not self._options.bell_on_activity:
            return
        sys.stdout.write("\a")
        sys.stdout.flush()

    def _signal_terminal_link_loss(self) -> None:
        """Mark the session as ended due to an unrecoverable disconnect and
        wake the prompt loop so ``run()`` returns immediately rather than
        waiting for the user to hit Enter."""
        if self.exit_reason is not None:
            return
        self.exit_reason = "terminal"
        self._stop.set()
        try:
            app = self._session.app
            if app.is_running:
                app.exit()
        except Exception:
            pass

    async def run(self) -> None:
        """Run until the user types ``/quit`` or the link drops."""
        with patch_stdout():
            print()
            if self._offline:
                print("Offline mode — browsing local store, no connection")
                print(
                    "/h for help, /quit to quit, /list to view stored "
                    "channels and DM threads"
                )
            else:
                print("Connected to WPS")
                print("/h for help, /quit to quit, /list to view channels")
            print()
            while not self._stop.is_set():
                try:
                    prompt_text = self._prompt_label()
                    line = await self._session.prompt_async(prompt_text)
                except (EOFError, KeyboardInterrupt):
                    break
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    if line.startswith("/"):
                        await self._handle_command(line)
                    else:
                        await self._send_to_target(line)
                except Exception as exc:  # surface to user without dying
                    print(f"[error] {exc}")

    def _print_channels(self) -> None:
        print("Channels: /ch <id> or /ch <name> to switch")
        print(" Subbed  ID   Name")
        rows = self._client._store.list_channels()  # type: ignore[attr-defined]
        names = {c.cid: c for c in self._channels}
        paused = self._client.paused_channels()
        seen: set[int] = set()
        entries: list[tuple[int, bool, ChannelInfo | None, int | None]] = []
        for r in rows:
            cid = r["cid"]
            seen.add(cid)
            entries.append(
                (cid, bool(r["subscribed"]), names.get(cid), paused.get(cid))
            )
        for c in self._channels:
            if c.cid in seen:
                continue
            entries.append((c.cid, False, c, paused.get(c.cid)))
        for cid, subscribed, info, paused_count in sorted(entries, key=lambda e: e[0]):
            self._print_channel_row(cid, subscribed, info, paused_count)

    def _print_channel_row(
        self,
        cid: int,
        subscribed: bool,
        info: ChannelInfo | None,
        paused_count: int | None,
    ) -> None:
        mark = "*" if subscribed else " "
        label = f"#{info.name}" if info and info.name else ""
        paused = f" ({paused_count} paused)" if paused_count else ""
        print(f"   [{mark}]   {cid:<3}  {label}{paused}")

    def _print_dm_threads(self) -> None:
        print("DM threads:  /dm <call> to switch")
        try:
            rows = self._client._store.list_dm_peers(self._my_call)  # type: ignore[attr-defined]
        except Exception:
            rows = []
        if not rows:
            print("  (no DM threads yet)")
            return
        for r in rows:
            print(f"  {self._fmt_user(r['peer'])}")

    def _show_history(
        self, target: tuple[str, str], n: int, *, verbose: bool | None = None
    ) -> None:
        """Print the last ``n`` historic messages/posts for ``target``.

        Pulled from the local SQLite store, oldest first. Silent if the
        store has nothing for that target — common right after a clean
        `--state-dir` or before any traffic has arrived for that peer.

        ``verbose`` defaults to the session option's ``verbose_history``;
        ``/vhistory`` passes ``True`` explicitly to force verbose for a
        one-shot replay.
        """
        if n <= 0:
            return
        if verbose is None:
            verbose = self._options.verbose_history
        kind, key = target
        store = self._client._store  # type: ignore[attr-defined]
        if kind == "dm":
            rows = store.recent_messages(key.upper(), limit=n)
            if not rows:
                return
            print(f"-- last {len(rows)} message(s) with {self._fmt_user(key)} --")
            for r in reversed(rows):
                if verbose:
                    print(self._fmt_dm_verbose(r))
                else:
                    self._print_dm(
                        r["from_call"], r["to_call"], r["ts"], r["body"],
                        reply_prefix=self._reply_prefix("dm", "", r),
                    )
        elif kind == "ch":
            rows = store.recent_posts(int(key), limit=n)
            if not rows:
                return
            print(f"-- last {len(rows)} post(s) in ch:{key} --")
            for r in reversed(rows):
                if verbose:
                    print(self._fmt_post_verbose(r))
                else:
                    self._print_post(
                        r["channel_id"], r["from_call"], r["ts"], r["body"],
                        reply_prefix=self._reply_prefix("ch", str(int(key)), r),
                    )

    def _render_dm(self, m: dict) -> None:
        """Render a freshly-arrived ``m``-shaped dict (from `m` or one
        element of an `mb` batch). The client's default handler ran
        before us and persisted the row; in verbose mode we look it up
        to recover the lid + receipt-time columns."""
        if self._options.verbose_history:
            row = self._lookup_message(m)
            if row is not None:
                print(self._fmt_dm_verbose(row))
                return
            # Row missing (race / external store). Fall through to
            # compact so something still appears.
        self._print_dm(
            m.get("fc"), m.get("tc"), m.get("ts"), m.get("m"),
            reply_prefix=self._reply_prefix("dm", "", m),
        )

    def _render_post(self, cid: int | None, p: dict) -> None:
        if self._options.verbose_history and cid is not None:
            row = self._lookup_post(int(cid), p)
            if row is not None:
                print(self._fmt_post_verbose(row))
                return
        target_key = str(int(cid)) if cid is not None else ""
        self._print_post(
            cid, p.get("fc"), p.get("ts"), p.get("p"),
            reply_prefix=self._reply_prefix("ch", target_key, p),
        )

    def _lookup_message(self, m: dict) -> dict | None:
        msg_id = m.get("_id") or (
            f"{m.get('ts')}-{m.get('fc')}" if m.get("ts") and m.get("fc") else None
        )
        if not msg_id:
            return None
        try:
            return self._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        except Exception:
            return None

    def _lookup_post(self, cid: int, p: dict) -> dict | None:
        ts = p.get("ts")
        if ts is None:
            return None
        try:
            return self._client._store.lookup_post(cid, int(ts))  # type: ignore[attr-defined]
        except Exception:
            return None

    def _print_dm(
        self,
        fc: str | None,
        tc: str | None,
        ts: int | float | None,
        body: str | None,
        *,
        reply_prefix: str = "",
    ) -> None:
        peer = tc if (fc or "").upper() == self._my_call.upper() else fc
        prefix = self._dm_prefix(peer or "")
        print(f"{prefix}{self._fmt_ts(ts)} {self._fmt_call(fc)}: {reply_prefix}{body}")

    def _print_post(
        self,
        cid: int | None,
        fc: str | None,
        ts: int | float | None,
        body: str | None,
        *,
        reply_prefix: str = "",
    ) -> None:
        prefix = self._channel_prefix(int(cid)) if cid is not None else ""
        print(f"{prefix}{self._fmt_ts(ts)} {self._fmt_call(fc)}: {reply_prefix}{body}")

    def _reply_prefix(self, kind: str, target_key: str, row: dict) -> str:
        """Plain-text reply preview prefix for one row, or ``""``.

        Looks up the parent in the store via the row's reply fields
        (``r`` for DMs, ``rts``/``rfc`` for posts). When the parent is
        in the local store, returns ``[Reply To CALL: 10chars...] ``;
        otherwise the ``<msg not in db>`` fallback variant.
        """
        store = self._client._store  # type: ignore[attr-defined]
        meta = resolve_reply_meta(store, kind, target_key, row)
        return reply_prefix_text(meta)

    def _fmt_dm_verbose(self, row: dict) -> str:
        """Verbose render of a DM row (from ``recent_messages`` or
        ``lookup_message_by_id``). Format:

            <prefix>ID: <lid> - [<ts>] - <middle>?<Name, CALL>: <body>

        The middle segment is omitted (and so is the trailing `" - "`)
        when nothing applies — i.e. inbound batch row, or outbound row
        for which we don't yet have an ack and the timeout hasn't yet
        elapsed (which still gets "Delivering...", but that's only "no
        middle" when the row predates the delivery columns).
        """
        fc = row.get("from_call")
        tc = row.get("to_call")
        peer = tc if (fc or "").upper() == self._my_call.upper() else fc
        prefix = self._dm_prefix(peer or "")
        reply_pref = self._reply_prefix("dm", "", row)
        return self._compose_verbose(prefix, row, reply_pref)

    def _fmt_post_verbose(self, row: dict) -> str:
        cid = row.get("channel_id")
        prefix = self._channel_prefix(int(cid)) if cid is not None else ""
        target_key = str(int(cid)) if cid is not None else ""
        reply_pref = self._reply_prefix("ch", target_key, row)
        return self._compose_verbose(prefix, row, reply_pref)

    def _compose_verbose(self, prefix: str, row: dict, reply_prefix: str = "") -> str:
        lid = row.get("lid")
        ts = row.get("ts")
        fc = row.get("from_call")
        body = row.get("body")
        middle = self._verbose_status(row)
        head = f"{prefix}ID: {lid} - {self._fmt_ts(ts)}"
        if middle:
            head = f"{head} - {middle}"
        return f"{head} - {self._fmt_call(fc)}: {reply_prefix}{body}"

    def _verbose_status(self, row: dict) -> str | None:
        """Compute the middle 'state' segment of the verbose line.

        Outbound (from us): Delivered/Delivering/NOT DELIVERED based on
        ``delivered_ts``, the row age, and the delivery-timeout option.
        Inbound: ``Received real-time in Xs`` when the row was first
        observed via the realtime path; otherwise no middle segment.

        ``ts`` may be in seconds (DM convention) or ms (post convention,
        plus legacy mixed data); the local clock is always ms. Normalise
        both before subtracting so durations are sane.
        """
        fc = (row.get("from_call") or "").upper()
        ts = row.get("ts")
        ts_ms = ts_to_ms(ts)
        if fc == self._my_call.upper():
            delivered = row.get("delivered_ts")
            if delivered is not None and ts_ms is not None:
                return f"Delivered to server in {self._fmt_duration_ms(int(delivered) - ts_ms)}"
            if ts_ms is None:
                return "Delivering..."
            age_ms = int(time.time() * 1000) - ts_ms
            timeout_ms = self._options.delivery_timeout_s * 1000
            if age_ms >= timeout_ms:
                return "NOT DELIVERED"
            return "Delivering..."
        if row.get("realtime") == 1 and row.get("received_ts") is not None and ts_ms is not None:
            delta = int(row["received_ts"]) - ts_ms
            return f"Received real-time in {self._fmt_duration_ms(delta)}"
        return None

    def _channel_prefix(self, cid: int) -> str:
        name = self._channel_name(cid)
        return f"{cid} #{name}> " if name else f"{cid}> "

    def _dm_prefix(self, peer: str) -> str:
        return f"dm {peer}> "

    @staticmethod
    def _fmt_ts(ts: int | float | None) -> str:
        ms = ts_to_ms(ts)
        if ms is None:
            return "[--]"
        dt = datetime.datetime.fromtimestamp(ms / 1000)
        return f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}]"

    @staticmethod
    def _ts_from_id(_id: str | None) -> int | None:
        # `mr` only carries `_id`, which the server documents as `{ts}-{fc}`
        # — pull ts back out so we can report the original send time.
        if not _id:
            return None
        try:
            return int(_id.split("-", 1)[0])
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _fmt_duration_ms(ms: int | float) -> str:
        s = max(0, round(ms / 1000))
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        return f"{m}m{s}s"

    @classmethod
    def _fmt_ack(
        cls,
        kind: str,
        label: str | None,
        lid: int | None,
        ts: int | float | None,
        dts: int | float | None = None,
    ) -> str:
        # cpr carries server-side dts; mr doesn't, so fall back to local
        # clock-at-receipt as the delivery instant.
        prefix = f"[ack] [{label}]" if label else "[ack]"
        suffix = f" {kind}{f' {lid}' if lid is not None else ''}"
        if ts is None:
            return f"{prefix}{suffix}"
        # Both ts and dts may have arrived in seconds (DM `mr` ack derives ts
        # from `_id` which is ms, but cpr's `ts`/`dts` follow post-vs-DM
        # convention). Normalise both to ms before subtracting.
        ts_ms = ts_to_ms(ts)
        end = ts_to_ms(dts) if dts is not None else time.time() * 1000
        return (
            f"{prefix}{suffix} at {cls._fmt_ts(ts)} "
            f"delivered in {cls._fmt_duration_ms(end - ts_ms)}"
        )

    def _fmt_mr_ack(self, obj: dict) -> str:
        msg_id = obj.get("_id")
        ts = self._ts_from_id(msg_id)
        if isinstance(msg_id, str):
            try:
                row = self._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
            except Exception:
                row = None
            if row is not None:
                peer = row.get("to_call") or row.get("from_call")
                label = f"dm:{peer}" if peer else None
                return self._fmt_ack("msg", label, row.get("lid"), ts)
        return self._fmt_ack("msg", None, None, ts)

    def _fmt_cpr_ack(self, obj: dict) -> str:
        ts = obj.get("ts")
        dts = obj.get("dts")
        if isinstance(ts, int):
            try:
                row = self._client._store.lookup_post_by_from_ts(  # type: ignore[attr-defined]
                    self._my_call, ts
                )
            except Exception:
                row = None
            if row is not None:
                cid = row.get("channel_id")
                name = self._channel_name(int(cid)) if isinstance(cid, int) else None
                label = (
                    f"ch:{cid} #{name}"
                    if name
                    else (f"ch:{cid}" if cid is not None else None)
                )
                return self._fmt_ack("post", label, row.get("lid"), ts, dts)
        return self._fmt_ack("post", None, None, ts, dts)

    def _fmt_call(self, call: str | None) -> str:
        """``<Name, CALL>`` for inline message attribution; ``<CALL>`` if
        the local hams table doesn't yet know a name for this callsign."""
        if not call:
            return ""
        name = self._client.ham_name(call)
        return f"<{name}, {call}>" if name else f"<{call}>"

    def _fmt_user(self, call: str | None) -> str:
        """``Name, CALL`` for roster/connect/disconnect notices; bare ``CALL``
        if no name is known."""
        if not call:
            return ""
        name = self._client.ham_name(call)
        return f"{name}, {call}" if name else str(call)

    def _prompt_label(self) -> str:
        offline = "(offline) " if self._offline else ""
        if self._target is None:
            return f"{offline}whatspyc> "
        kind, key = self._target
        if kind == "dm":
            return f"{offline}dm {key}> "
        if kind == "ch":
            cid = int(key)
            name = self._channel_name(cid)
            return f"{offline}{cid} #{name}> " if name else f"{offline}{cid}> "
        return f"{offline}{kind}:{key}> "

    def _refuse_offline(self, what: str) -> bool:
        """If running offline, print a one-line hint and return ``True``.

        Used as an early-out guard in send paths and any slash command
        that needs the wire link. Read-only paths (history, listings,
        target switching) skip this check entirely.
        """
        if not self._offline:
            return False
        print(f"[offline] {what} unavailable — read-only mode (no connection)")
        return True

    def _channel_name(self, cid: int) -> str | None:
        for c in self._channels:
            if c.cid == cid and c.name:
                return c.name
        return None

    def _channel_ref(self, cid: int) -> str:
        """``ch 5 #lounge`` if the directory has a name for ``cid``,
        else ``ch 5`` — used to label channel references inside the
        bracketed status lines."""
        name = self._channel_name(cid)
        return f"ch {cid} #{name}" if name else f"ch {cid}"

    def _fmt_delivery_timeout(self, obj: dict) -> str:
        """Render a ``_delivery_timeout`` synthetic event.

        Same shape regardless of the ``show_acks`` setting — the timeout
        notice is always informative, since by definition the user
        didn't get an ack to confirm delivery. ``is_edit`` adds an
        explicit "(edit)" tag so the user knows it's the edit's ack
        that's missing rather than the original send's; either way the
        same /retrydm or /retrypost command does the right thing
        (resend_message and resend_post dispatch on edit_ts)."""
        kind = obj.get("kind")
        lid = obj.get("lid")
        ts = self._fmt_ts(obj.get("ts"))
        edit_tag = " (edit)" if obj.get("is_edit") else ""
        if kind == "post":
            cid = obj.get("cid")
            name = self._channel_name(int(cid)) if isinstance(cid, int) else None
            ref = f"ch:{cid} #{name}" if name else f"ch:{cid}"
            return (
                f"[timeout] [{ref}] post {lid}{edit_tag} at {ts}. "
                f"To resend: /retrypost {lid}"
            )
        peer = obj.get("peer")
        return (
            f"[timeout] [dm:{peer}] msg {lid}{edit_tag} at {ts}. "
            f"To resend: /retrydm {lid}"
        )

    def _fmt_dm_edit(self, obj: dict) -> str | None:
        """Render a real-time ``med`` frame as
        ``dm <peer>> [<edts>] <Name, CALL>: [EDITED] <new body>``.

        Returns ``None`` if we can't resolve the local row — i.e. the
        edit landed for a message that predates our local store. There
        isn't enough context in the wire frame alone to render
        anything useful (no ``tc`` / ``ts``), so we silently drop the
        notification rather than print a stub line."""
        msg_id = obj.get("_id")
        if not isinstance(msg_id, str):
            return None
        row = self._client._store.lookup_message_by_id(msg_id)  # type: ignore[attr-defined]
        if row is None:
            return None
        fc = row.get("from_call")
        tc = row.get("to_call")
        peer = tc if (fc or "").upper() == self._my_call.upper() else fc
        prefix = self._dm_prefix(peer or "")
        edts = obj.get("edts") or row.get("edit_ts")
        body = obj.get("m", row.get("body", ""))
        reply_pref = self._reply_prefix("dm", "", row)
        return (
            f"{prefix}{self._fmt_ts(edts)} {self._fmt_call(fc)}: "
            f"{reply_pref}[EDITED] {body}"
        )

    def _fmt_post_edit(self, obj: dict) -> str | None:
        """Render a real-time ``cped`` frame as
        ``<cid> #<name>> [<edts>] <Name, CALL>: [EDITED] <new body>``.

        Returns ``None`` for an edit on a post we don't have locally —
        same reasoning as :meth:`_fmt_dm_edit`."""
        cid = obj.get("cid")
        ts = obj.get("ts")
        if not isinstance(cid, int) or not isinstance(ts, int):
            return None
        row = self._client._store.lookup_post(int(cid), int(ts))  # type: ignore[attr-defined]
        if row is None:
            return None
        prefix = self._channel_prefix(int(cid))
        edts = obj.get("edts") or row.get("edit_ts")
        body = obj.get("p", row.get("body", ""))
        fc = row.get("from_call")
        reply_pref = self._reply_prefix("ch", str(int(cid)), row)
        return (
            f"{prefix}{self._fmt_ts(edts)} {self._fmt_call(fc)}: "
            f"{reply_pref}[EDITED] {body}"
        )

    def _paused_hint(self, cid: int, paused: int) -> str:
        return (
            f"[{self._channel_ref(cid)} is paused — {paused} posts waiting "
            f"on the server. Run /unpause {cid} [N] to download them. "
            f"Posting is blocked until you unpause.]"
        )

    async def _send_to_target(self, text: str) -> None:
        if self._refuse_offline("sending"):
            return
        if self._target is None:
            print("[hint] no current target. use /dm CALL or /ch N|#NAME")
            return
        kind, key = self._target
        if kind == "dm":
            await self._client.send_message(key, text)
        elif kind == "ch":
            cid = int(key)
            name = self._channel_name(cid)
            if name and name.lower() == "announcements":
                # Web client marks #announcements as ro (read-only) and
                # blocks posts client-side; the server doesn't enforce
                # it, so we have to mirror the block ourselves.
                print("[Users cannot post to #announcements]")
                return
            if not self._is_subscribed(cid):
                # The server accepts cp from non-subscribers and will
                # broadcast to subscribers, but won't echo anything else
                # back to us — so the conversation is one-way. Block.
                print(self._unsubscribed_send_hint(cid))
                return
            paused = self._client.paused_channels().get(cid)
            if paused:
                # Posting itself works server-side, but the user can't see
                # the recent context they'd be replying to — block to avoid
                # them talking past whatever's in the 700-post backlog.
                print(self._paused_hint(cid, paused))
                return
            await self._client.post(cid, text)

    def _is_subscribed(self, cid: int) -> bool:
        try:
            rows = self._client._store.list_channels()  # type: ignore[attr-defined]
        except Exception:
            return False
        for r in rows:
            if r["cid"] == cid:
                return bool(r["subscribed"])
        return False

    def _unsubscribed_send_hint(self, cid: int) -> str:
        return f"[{self._channel_ref(cid)}] not subscribed"

    def _known_cids(self) -> set[int]:
        """Cids the user is allowed to target — channel directory plus
        anything the local store has ever seen (subscriptions, posts).
        """
        cids = {c.cid for c in self._channels}
        try:
            cids.update(r["cid"] for r in self._client._store.list_channels())  # type: ignore[attr-defined]
        except Exception:
            pass
        return cids

    def _resolve_channel(self, arg: str, *, allow_unknown_cid: bool = False) -> int | None:
        """Turn a channel argument into a channel id.

        Accepts a numeric cid, a `#name`, or a bare `name` from the
        channel directory (case-insensitive). A leading `#` is optional
        — `lounge` and `#lounge` both work. A numeric cid wins over a
        same-named entry: `5` resolves as cid 5 even if some channel is
        also named `"5"`.

        Returns ``None`` when the name doesn't match the directory, or
        when the cid is unknown and ``allow_unknown_cid`` is False (the
        local store and the directory between them define "known").
        ``/sub`` passes ``allow_unknown_cid=True`` so it can discover
        cids that aren't yet in the directory.
        """
        if arg.startswith("#"):
            wanted = arg[1:].lower()
        else:
            try:
                cid = int(arg)
            except ValueError:
                wanted = arg.lower()
            else:
                if allow_unknown_cid or cid in self._known_cids():
                    return cid
                return None
        for c in self._channels:
            if c.name and c.name.lower() == wanted:
                return c.cid
        return None

    async def _handle_unpause(self, args: list[str]) -> None:
        """``/unpause CID|#name [N]`` — clear the server-side pause flag
        and download the last ``N`` historic posts. Without ``N``, falls
        back to the pending count from the most recent ``pch`` header.
        """
        if self._refuse_offline("/unpause"):
            return
        cid = self._resolve_channel(args[0])
        if cid is None:
            print(f"[hint] /unpause: unknown channel {args[0]!r} (use cid or #name)")
            return
        if len(args) == 2:
            try:
                n = int(args[1])
            except ValueError:
                print(f"[hint] /unpause: post count must be an integer, got {args[1]!r}")
                return
            if n <= 0:
                print("[hint] /unpause: post count must be positive")
                return
        else:
            n = self._client.paused_channels().get(cid, 0)
            if n <= 0:
                print(
                    f"[hint] /unpause cid={cid}: no pending count from pch "
                    f"headers; pass an explicit /unpause {cid} N"
                )
                return
        await self._client.unpause_channel(cid, post_count=n)
        print(f"[unpause] requested {n} post(s) for {self._channel_ref(cid)}")

    async def _handle_sub(self, args: list[str]) -> None:
        """``/sub CID|#NAME [N]`` — subscribe to a channel and (optionally)
        pull ``N`` historic posts.

        Without ``N``, the flow is two-phase:

          1. Send `cs` and wait for the server's ack (which carries the
             count of historic posts available).
          2. Prompt the user for how many to fetch, defaulting to
             ``auto_backfill_post_count`` if set, else 10.
          3. Fire `cpb` for the chosen count (if positive).

        With ``N``, skip the prompt entirely. ``N=0`` subscribes without
        fetching anything (realtime-only from now on).
        """
        if self._refuse_offline("/sub"):
            return
        cid = self._resolve_channel(args[0], allow_unknown_cid=True)
        if cid is None:
            print(f"[hint] /sub: unknown channel {args[0]!r} (use cid or #name)")
            return

        explicit_n: int | None = None
        if len(args) == 2:
            try:
                explicit_n = int(args[1])
            except ValueError:
                print(f"[hint] /sub: post count must be an integer, got {args[1]!r}")
                return
            if explicit_n < 0:
                print("[hint] /sub: post count must be non-negative")
                return

        try:
            pc = await self._client.subscribe_and_wait(cid)
        except asyncio.TimeoutError:
            print(f"[hint] /sub: timed out waiting for ack for {self._channel_ref(cid)}")
            return

        if pc <= 0:
            return  # nothing historic to fetch, render_event already noted "subscribed"

        if explicit_n is not None:
            n = explicit_n
        else:
            default = self._client.auto_backfill_post_count or 10
            default = min(default, pc)
            n = await self._prompt_for_count(
                f"Load how many historic posts? [{default}]: ",
                default=default,
            )

        if n <= 0:
            return
        await self._client.request_post_batch(cid, min(n, pc))

    async def _prompt_for_count(self, prompt_text: str, *, default: int) -> int:
        """Ask for an integer count via the prompt-toolkit session.

        Empty input → ``default``. Non-integer input → ``default`` with a
        warning. Pulled out as a method so tests can monkey-patch it.
        """
        raw = (await self._session.prompt_async(prompt_text)).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"[hint] expected an integer, got {raw!r} — using default {default}")
            return default

    async def _prompt_yes_no(self, prompt_text: str, *, default: bool = False) -> bool:
        """Ask a yes/no question. Empty input → ``default``. ``y``/``yes``
        → True, anything else → False. Pulled out as a method so tests can
        monkey-patch it."""
        raw = (await self._session.prompt_async(prompt_text)).strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")

    def _handle_help(self, args: list[str]) -> None:
        """``/h`` lists every slash command. ``/h <command>`` shows
        detailed help for one — the leading slash is optional."""
        if not args:
            for line in help_data.list_lines():
                print(line)
            return
        lines = help_data.detail_lines(args[0])
        if lines is None:
            print(
                f"[hint] /h: unknown command {args[0]!r}. "
                f"Try /h with no arguments for the full list."
            )
            return
        for line in lines:
            print(line)

    def _handle_set(self, args: list[str]) -> None:
        """``/set`` (no args) lists every option; ``/set NAME`` shows one;
        ``/set NAME VALUE`` updates one for the running session."""
        if not args:
            print("Session settings (use /set NAME VALUE to change):")
            for n in self._options.names():
                print(
                    f"  {n} = {self._options.format(n)}"
                    f"   {self._options.describe(n)}"
                )
            return
        name = args[0]
        if name not in self._options.names():
            print(
                f"[hint] /set: unknown option {name!r}. "
                f"Known: {', '.join(self._options.names())}"
            )
            return
        if len(args) == 1:
            print(f"{name} = {self._options.format(name)}")
            return
        try:
            old, new = self._options.set(name, " ".join(args[1:]))
        except ValueError as exc:
            print(f"[hint] /set {name}: {exc}")
            return
        # The client owns the per-row timeout timers, so changes to
        # delivery_timeout_s have to flow through to it as well — future
        # sends use the new value; in-flight timers keep their original
        # delay (they were scheduled with the value current at send time).
        if name == "delivery_timeout_s":
            self._client.set_delivery_timeout_s(new)
        old_fmt = self._options.format_value(name, old)
        new_fmt = self._options.format_value(name, new)
        if old_fmt == new_fmt:
            print(f"{name} = {new_fmt} (unchanged)")
        else:
            print(f"{name} = {new_fmt} (was {old_fmt})")

    async def _handle_command(self, line: str) -> None:
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        if cmd == "/quit":
            await self._client.close()
            self._stop.set()
        elif cmd == "/h" and len(args) <= 1:
            self._handle_help(args)
        elif cmd == "/sub" and 1 <= len(args) <= 2:
            await self._handle_sub(args)
        elif cmd == "/unsub" and len(args) == 1:
            if self._refuse_offline("/unsub"):
                return
            cid = self._resolve_channel(args[0], allow_unknown_cid=True)
            if cid is None:
                print(f"[hint] /unsub: unknown channel {args[0]!r} (use cid or #name)")
                return
            await self._client.unsubscribe(cid)
        elif cmd == "/unpause" and 1 <= len(args) <= 2:
            await self._handle_unpause(args)
        elif cmd == "/list":
            which = args[0].lower() if args else "all"
            if which not in ("all", "ch", "dm"):
                print(f"[hint] /list takes 'ch' or 'dm' (got {args[0]!r})")
                return
            if which in ("all", "ch"):
                self._print_channels()
            if which == "all":
                print()
            if which in ("all", "dm"):
                self._print_dm_threads()
        elif cmd == "/users":
            users = self._client.online_users()
            if not users:
                print("  (no users online — or not yet seen the connect roster)")
            else:
                print(f"online ({len(users)}):")
                for call in users:
                    print(f"  {self._fmt_user(call)}")
        elif cmd == "/editdm" and len(args) >= 2:
            if self._refuse_offline("/editdm"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /editdm: LID must be an integer (got {args[0]!r})")
                return
            row = self._client._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                print(f"[hint] /editdm: no local message with lid {lid}")
                return
            try:
                await self._client.edit_message(row["id"], " ".join(args[1:]))
            except ValueError as exc:
                print(f"[{exc}]")
                return
        elif cmd == "/editpost" and len(args) >= 2:
            if self._refuse_offline("/editpost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /editpost: LID must be an integer (got {args[0]!r})")
                return
            row = self._client._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                print(f"[hint] /editpost: no local post with lid {lid}")
                return
            try:
                await self._client.edit_post(
                    row["channel_id"], row["ts"], " ".join(args[1:])
                )
            except ValueError as exc:
                print(f"[{exc}]")
                return
        elif cmd == "/retrydm" and len(args) == 1:
            if self._refuse_offline("/retrydm"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /retrydm: LID must be an integer (got {args[0]!r})")
                return
            row = self._client._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                print(f"[hint] /retrydm: no local message with lid {lid}")
                return
            try:
                await self._client.resend_message(row["id"])
            except ValueError as exc:
                print(f"[{exc}]")
                return
            print(f"[retrydm] resent lid {lid}")
        elif cmd == "/retrypost" and len(args) == 1:
            if self._refuse_offline("/retrypost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /retrypost: LID must be an integer (got {args[0]!r})")
                return
            row = self._client._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                print(f"[hint] /retrypost: no local post with lid {lid}")
                return
            try:
                await self._client.resend_post(row["channel_id"], row["ts"])
            except ValueError as exc:
                print(f"[{exc}]")
                return
            print(f"[retrypost] resent lid {lid}")
        elif cmd == "/replydm" and len(args) >= 2:
            if self._refuse_offline("/replydm"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /replydm: LID must be an integer (got {args[0]!r})")
                return
            row = self._client._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                print(f"[hint] /replydm: no local message with lid {lid}")
                return
            # Reply goes to whichever side of the parent thread isn't us.
            peer_call = (
                row["to_call"]
                if (row["from_call"] or "").upper() == self._my_call.upper()
                else row["from_call"]
            )
            await self._client.send_message(
                peer_call, " ".join(args[1:]), reply_id=row["id"]
            )
        elif cmd == "/replypost" and len(args) >= 2:
            if self._refuse_offline("/replypost"):
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /replypost: LID must be an integer (got {args[0]!r})")
                return
            row = self._client._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
            if row is None:
                print(f"[hint] /replypost: no local post with lid {lid}")
                return
            cid = int(row["channel_id"])
            paused = self._client.paused_channels().get(cid)
            if paused:
                print(self._paused_hint(cid, paused))
                return
            await self._client.post(
                cid,
                " ".join(args[1:]),
                reply_ts=int(row["ts"]),
                reply_from=row["from_call"],
            )
        elif cmd == "/react" and len(args) == 2:
            if self._refuse_offline("/react"):
                return
            if self._target is None:
                print("[hint] /react: no current target. /dm CALL or /ch N|#NAME first")
                return
            try:
                lid = int(args[0])
            except ValueError:
                print(f"[hint] /react: ID must be an integer (got {args[0]!r})")
                return
            kind, _ = self._target
            if kind == "dm":
                row = self._client._store.lookup_message_by_lid(lid)  # type: ignore[attr-defined]
                if row is None:
                    print(f"[hint] /react: no local message with lid {lid}")
                    return
                await self._client.react_message(row["id"], args[1])
            else:
                row = self._client._store.lookup_post_by_lid(lid)  # type: ignore[attr-defined]
                if row is None:
                    print(f"[hint] /react: no local post with lid {lid}")
                    return
                await self._client.react_post(row["channel_id"], row["ts"], args[1])
        elif cmd == "/dm" and len(args) == 1:
            self._target = ("dm", args[0].upper())
            self._show_history(self._target, self._history_backfill)
        elif cmd == "/ch" and len(args) == 1:
            cid = self._resolve_channel(args[0])
            if cid is None:
                print(f"[hint] /ch: unknown channel {args[0]!r} (use cid or #name)")
                return
            previous_target = self._target
            self._target = ("ch", str(cid))
            # History (from local store) goes first so the user has whatever
            # context they already have on screen, then the paused notice
            # or subscribe prompt sits right above the prompt where it
            # can't be missed.
            self._show_history(self._target, self._history_backfill)
            paused = self._client.paused_channels().get(cid)
            if paused:
                # Paused implies subscribed (the server only flags channels
                # you're already subscribed to via pch), so the subscribe
                # prompt below is unreachable from here.
                print(self._paused_hint(cid, paused))
            elif not self._is_subscribed(cid) and not self._offline:
                if await self._prompt_yes_no(
                    f"[{self._channel_ref(cid)}] Not subscribed. "
                    f"Subscribe now? [y/N]: ",
                    default=False,
                ):
                    await self._handle_sub([str(cid)])
                else:
                    self._target = previous_target
        elif cmd == "/set":
            self._handle_set(args)
        elif cmd == "/history":
            if self._target is None:
                print("[hint] no current target. /dm CALL or /ch N|#NAME first")
                return
            try:
                n = int(args[0]) if args else self._history_backfill
            except ValueError:
                print(f"[hint] /history takes an integer count, got {args[0]!r}")
                return
            if n <= 0:
                print("[hint] /history N: N must be a positive integer")
                return
            self._show_history(self._target, n)
        elif cmd == "/vhistory":
            if self._target is None:
                print("[hint] no current target. /dm CALL or /ch N|#NAME first")
                return
            try:
                n = int(args[0]) if args else self._history_backfill
            except ValueError:
                print(f"[hint] /vhistory takes an integer count, got {args[0]!r}")
                return
            if n <= 0:
                print("[hint] /vhistory N: N must be a positive integer")
                return
            self._show_history(self._target, n, verbose=True)
        else:
            print(f"[hint] unknown or malformed command: {line}")
