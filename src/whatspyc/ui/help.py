"""Slash-command help data shared by ``LineUI`` and ``TextualUI``.

Each entry has a ``usage`` (terse syntax shown in the listing), a
``summary`` (one-liner) and a ``details`` block (multi-paragraph). The
listing and detail formatters return plain text lines so each UI can
print them however it likes — ``LineUI`` to stdout, ``TextualUI`` to its
``RichLog``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandHelp:
    name: str
    usage: str
    summary: str
    details: str


COMMANDS: dict[str, CommandHelp] = {
    "/h": CommandHelp(
        name="/h",
        usage="/h [command]",
        summary="Show help for slash commands.",
        details=(
            "/h with no arguments lists every slash command with a "
            "one-line summary.\n"
            "\n"
            "/h <command> shows detailed help for a specific command. "
            "The leading slash is optional, so '/h ch' and '/h /ch' "
            "are equivalent."
        ),
    ),
    "/dm": CommandHelp(
        name="/dm",
        usage="/dm CALL",
        summary="Set the current target to a DM with CALL.",
        details=(
            "Sets the implicit recipient for plain-text messages to a "
            "direct-message thread with CALL (uppercased automatically). "
            "Pulls the last 'history_backfill' messages with that peer "
            "from the local store as a preview. Plain text typed at the "
            "prompt afterwards is sent to that DM until you switch with "
            "another /dm or /ch."
        ),
    ),
    "/ch": CommandHelp(
        name="/ch",
        usage="/ch ID|NAME",
        summary="Set the current target to a channel by id or name.",
        details=(
            "Switches the current target to a channel. Accepts either a "
            "numeric channel id (/ch 5) or a name from the channel "
            "directory (/ch lounge or /ch #lounge — the leading # is "
            "optional, lookup is case-insensitive).\n"
            "\n"
            "After switching, the last 'history_backfill' posts in that "
            "channel are replayed from the local store. If the channel "
            "is paused on the server, a hint is printed; if you aren't "
            "subscribed, the UI offers to subscribe — declining drops "
            "you back to whatever target you had before."
        ),
    ),
    "/sub": CommandHelp(
        name="/sub",
        usage="/sub ID|NAME [N]",
        summary="Subscribe to a channel and pull N historic posts.",
        details=(
            "Subscribes to a channel and (optionally) pulls N historic "
            "posts. The channel can be a numeric cid or a directory "
            "name (with or without a leading #, case-insensitive).\n"
            "\n"
            "Without N this is a two-phase flow: send the subscribe "
            "request, wait for the server's ack carrying the count of "
            "available historic posts, then prompt for how many to "
            "fetch (defaulting to 'auto_backfill_post_count' if "
            "configured, else 10, capped at the actual count).\n"
            "\n"
            "With N, no prompt — fetches exactly N posts. N=0 "
            "subscribes without backfill (realtime-only)."
        ),
    ),
    "/unsub": CommandHelp(
        name="/unsub",
        usage="/unsub ID|NAME",
        summary="Unsubscribe from a channel.",
        details=(
            "Sends a server-side unsubscribe for the channel. New posts "
            "in that channel will no longer be relayed to you. Local "
            "history is left untouched. The channel can be referenced "
            "by cid or by a directory name (with or without a leading "
            "#, case-insensitive)."
        ),
    ),
    "/unpause": CommandHelp(
        name="/unpause",
        usage="/unpause ID|NAME [N]",
        summary="Clear the pause flag on a channel and pull last N posts.",
        details=(
            "When a subscribed channel has more pending posts than the "
            "server is willing to push at once, it sends a 'pch' header "
            "marking the channel as paused. /unpause clears that flag "
            "and downloads the last N posts.\n"
            "\n"
            "Without N, defaults to the pending count from the most "
            "recent pch header. With N (positive integer), downloads "
            "exactly N posts. The channel can be referenced by cid or "
            "by a directory name (with or without a leading #, "
            "case-insensitive)."
        ),
    ),
    "/list": CommandHelp(
        name="/list",
        usage="/list [ch|dm]",
        summary="List channels and/or DM threads.",
        details=(
            "/list (no argument) shows subscribed channels first, then "
            "any configured channel-directory entries you aren't "
            "subscribed to, then any saved DM threads.\n"
            "\n"
            "/list ch restricts the listing to channels.\n"
            "/list dm restricts the listing to DM threads."
        ),
    ),
    "/users": CommandHelp(
        name="/users",
        usage="/users",
        summary="List callsigns currently online.",
        details=(
            "Prints the cached online roster — seeded by the type-'o' "
            "payload at connect, then kept in sync by uc/ud events. "
            "Cleared and re-seeded on every reconnect. Names from the "
            "local hams table are shown alongside the callsign when "
            "known."
        ),
    ),
    "/editdm": CommandHelp(
        name="/editdm",
        usage="/editdm ID text...",
        summary="Edit a DM you previously sent.",
        details=(
            "Edits an existing direct message. ID is the local short id "
            "shown in the message log (a small integer — the SQLite "
            "rowid in the local store). Everything after ID is the new "
            "body.\n"
            "\n"
            "The lid is not portable across machines or state-dir "
            "rebuilds — it's a session-local handle. Internally /editdm "
            "translates it to the server's `_id` (`{ts}-{fc}`) before "
            "sending the `med` frame."
        ),
    ),
    "/editpost": CommandHelp(
        name="/editpost",
        usage="/editpost ID text...",
        summary="Edit a channel post you previously sent.",
        details=(
            "Edits an existing channel post. ID is the local short id "
            "shown in the post log (a small integer — the SQLite rowid "
            "in the local store). Everything after ID is the new body.\n"
            "\n"
            "Posts have no server-side identifier — they're keyed on "
            "`(cid, ts)`. /editpost looks up that pair via the lid and "
            "sends the corresponding `cped` frame."
        ),
    ),
    "/retrydm": CommandHelp(
        name="/retrydm",
        usage="/retrydm ID",
        summary="Resend a DM (or DM edit) that hasn't been acked yet.",
        details=(
            "Re-sends a previously-sent direct message. ID is the local "
            "short id shown in the message log (a small integer — the "
            "SQLite rowid in the local store).\n"
            "\n"
            "Edit-aware: if the row has been edited since it was first "
            "sent (`edit_ts IS NOT NULL`), /retrydm re-emits the latest "
            "edit (`med` frame) — that's almost always what the user "
            "actually wants. Otherwise it re-emits the original `m`.\n"
            "\n"
            "Either form is server-idempotent — the server dedupes on "
            "`_id` and just re-acks. Useful when the verbose render "
            "flips a row to 'NOT DELIVERED' or a `[timeout]` notice "
            "appeared. Refused on rows you didn't send."
        ),
    ),
    "/retrypost": CommandHelp(
        name="/retrypost",
        usage="/retrypost ID",
        summary="Resend a post (or post edit) that hasn't been acked yet.",
        details=(
            "Re-sends a previously-sent channel post. ID is the local "
            "short id shown in the post log (a small integer — the "
            "SQLite rowid in the local store).\n"
            "\n"
            "Edit-aware: if the post has been edited, /retrypost "
            "re-emits the latest edit (`cped` frame) instead of the "
            "original `cp`. Either form is server-idempotent — the "
            "server dedupes on `(cid, ts)` and re-emits the `cpr` ack. "
            "Refused on rows you didn't send."
        ),
    ),
    "/react": CommandHelp(
        name="/react",
        usage="/react ID CODEPOINT",
        summary="React to a message or post with an emoji.",
        details=(
            "Adds an emoji reaction. ID is the local short id shown in "
            "the log (the SQLite rowid in the local store). CODEPOINT "
            "is the unicode codepoint in hex, e.g. '1f44d' for "
            "thumbs-up.\n"
            "\n"
            "Dispatches on the current target: in a DM target the id "
            "is looked up in the messages table and a `mem` frame is "
            "sent; in a channel target the id is looked up in the "
            "posts table and a `cpem` frame is sent. /dm or /ch first "
            "if no target is set."
        ),
    ),
    "/history": CommandHelp(
        name="/history",
        usage="/history [N]",
        summary="Show messages in compact form (TUI: refresh in place; line: replay N).",
        details=(
            "Line UI: re-prints the last N messages (DM target) or "
            "posts (channel target) from the local store. Without N, "
            "uses the configured 'history_backfill' value. Output "
            "style follows the 'verbose_history' session option.\n"
            "\n"
            "TUI: the centre pane already shows history (and pages "
            "older on cursor-up), so /history doesn't replay. Instead "
            "it sets verbose_history = false and refreshes every "
            "mounted row in place — the counterpart to /vhistory. The "
            "[N] argument is silently ignored."
        ),
    ),
    "/vhistory": CommandHelp(
        name="/vhistory",
        usage="/vhistory [N]",
        summary="Show messages in verbose form (TUI: refresh in place; line: one-shot replay).",
        details=(
            "Line UI: like /history but always renders the verbose "
            "form (local id, timestamp, delivery state for outbound, "
            "realtime-receipt latency for inbound) regardless of the "
            "'verbose_history' session option. Does not change the "
            "option.\n"
            "\n"
            "TUI: equivalent to Ctrl+D but absolute — sets "
            "verbose_history = true and refreshes every mounted row "
            "in place. The [N] argument is silently ignored (use "
            "cursor-up at the top of the message list to page older)."
        ),
    ),
    "/set": CommandHelp(
        name="/set",
        usage="/set [NAME [VALUE]]",
        summary="View or change session-tunable options.",
        details=(
            "/set with no arguments lists every known option with its "
            "current value and a one-line description.\n"
            "/set NAME shows the current value for one option.\n"
            "/set NAME VALUE updates the option for the running "
            "session — no persistence; restarting picks up the config "
            "value again.\n"
            "\n"
            "Booleans accept on/off, true/false, yes/no, 1/0."
        ),
    ),
    "/quit": CommandHelp(
        name="/quit",
        usage="/quit",
        summary="Disconnect and exit cleanly.",
        details=(
            "Closes the local AX.25 link to the entry node. The rest "
            "of the chain tears down through normal protocol behaviour."
        ),
    ),
}


def normalize(name: str) -> str:
    """``ch``, ``/ch``, ``CH`` → ``/ch``. Empty string for blank input."""
    n = name.strip().lower()
    if not n:
        return ""
    if not n.startswith("/"):
        n = "/" + n
    return n


def list_lines(hide: set[str] | None = None) -> list[str]:
    """Lines for ``/h`` with no argument: a table-like listing of every
    command with its terse usage and one-line summary.

    ``hide`` lists command names (with or without leading ``/``) to omit
    from the listing — used by the TUI to drop commands it has replaced
    with GUI affordances (``/list`` → channel list pane, ``/users`` →
    online list, ``/set`` → settings modal handles its own UI)."""
    hidden = {normalize(n) for n in (hide or set())}
    visible = [c for c in COMMANDS.values() if normalize(c.name) not in hidden]
    if not visible:
        return ["Slash commands (use /h <command> for details):"]
    width = max(len(c.usage) for c in visible)
    out = ["Slash commands (use /h <command> for details):"]
    for c in visible:
        out.append(f"  {c.usage:<{width}}   {c.summary}")
    return out


def detail_lines(name: str) -> list[str] | None:
    """Lines for ``/h <name>``. Returns ``None`` when the command is
    unknown so the UI can surface a hint."""
    cmd = COMMANDS.get(normalize(name))
    if cmd is None:
        return None
    out = [
        f"{cmd.name} — {cmd.summary}",
        f"usage: {cmd.usage}",
        "",
    ]
    out.extend(cmd.details.splitlines())
    return out
