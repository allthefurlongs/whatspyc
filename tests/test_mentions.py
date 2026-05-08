"""@-mention plumbing.

Covers:

* the input parser (``parse_post_mentions``) — strips leading
  ``@CALL`` tokens off a post body and yields them as an array of
  callsigns, mirroring what the web client's @-picker produces.
* the row-side helpers (``at_calls_from_row``, ``at_calls_prefix``)
  used by the line / textual / urwid render paths.
* the wire side (``WpsClient.post`` accepts ``at_calls`` and emits
  the protocol's ``at`` field; ``resend_post`` re-emits it).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from whatspyc.store.store import SqliteStore
from whatspyc.ui import (
    at_calls_from_row,
    at_calls_prefix,
    parse_post_mentions,
)
from whatspyc.wps.client import WpsClient


def test_parse_post_mentions_extracts_leading_at_tokens() -> None:
    body, ats = parse_post_mentions("@2E0HKD @M6HKD hello there")
    assert body == "hello there"
    assert ats == ["2E0HKD", "M6HKD"]


def test_parse_post_mentions_uppercases_and_dedupes() -> None:
    body, ats = parse_post_mentions("@m0abc @M0ABC @g7bar plain")
    assert body == "plain"
    assert ats == ["M0ABC", "G7BAR"]


def test_parse_post_mentions_supports_ssid() -> None:
    body, ats = parse_post_mentions("@2E0HKD-2 @M6HKD-12 ping")
    assert body == "ping"
    assert ats == ["2E0HKD-2", "M6HKD-12"]


def test_parse_post_mentions_stops_at_first_non_mention() -> None:
    """``@CALL`` mid-body is left as plain text — only leading runs
    are consumed."""
    body, ats = parse_post_mentions("@M0ABC hello @G7BAR is busy")
    assert body == "hello @G7BAR is busy"
    assert ats == ["M0ABC"]


def test_parse_post_mentions_no_mentions_returns_body_unchanged() -> None:
    body, ats = parse_post_mentions("just a normal post with no mentions")
    assert body == "just a normal post with no mentions"
    assert ats == []


def test_parse_post_mentions_empty_input() -> None:
    body, ats = parse_post_mentions("")
    assert body == ""
    assert ats == []


def test_parse_post_mentions_only_mentions_yields_empty_body() -> None:
    body, ats = parse_post_mentions("@M0ABC @G7BAR")
    assert body == ""
    assert ats == ["M0ABC", "G7BAR"]


def test_parse_post_mentions_rejects_non_callsign_token() -> None:
    """``@123`` (no letters at all) is still a valid AX.25-shaped
    token under the loose 1–6-alphanumeric rule, but ``@@foo`` and
    ``@`` alone should not consume any tokens."""
    body, ats = parse_post_mentions("@@nope hi")
    assert body == "@@nope hi"
    assert ats == []
    body, ats = parse_post_mentions("@ hi")
    assert body == "@ hi"
    assert ats == []


def test_at_calls_from_row_decodes_json_string() -> None:
    row = {"at_calls": json.dumps(["M0ABC", "G7BAR"])}
    assert at_calls_from_row(row) == ["M0ABC", "G7BAR"]


def test_at_calls_from_row_handles_missing_and_malformed() -> None:
    assert at_calls_from_row({}) == []
    assert at_calls_from_row({"at_calls": None}) == []
    assert at_calls_from_row({"at_calls": ""}) == []
    assert at_calls_from_row({"at_calls": "not json"}) == []
    # A list value (e.g. an in-memory row that wasn't sourced from
    # SQLite) round-trips without re-encoding through JSON.
    assert at_calls_from_row({"at_calls": ["m0abc"]}) == ["M0ABC"]


def test_at_calls_prefix_formats_tags_with_trailing_space() -> None:
    assert at_calls_prefix(["M0ABC", "G7BAR"]) == "[@M0ABC] [@G7BAR] "
    assert at_calls_prefix([]) == ""


def _build_client(tmp_path: Path) -> tuple[WpsClient, SqliteStore, list[dict]]:
    """Construct a ``WpsClient`` whose ``_send`` is patched to record
    every frame it would have put on the wire. Skips the open / login
    handshake — these tests only care about frame composition, not the
    transport layer."""
    sent: list[dict] = []

    class _UnusedStream:
        injects_callsign = False

        async def open(self) -> None:  # pragma: no cover
            return None

        async def close(self) -> None:  # pragma: no cover
            return None

        async def send(self, data: bytes) -> None:  # pragma: no cover
            return None

        async def recv(self) -> bytes:  # pragma: no cover
            return b""

    store = SqliteStore(tmp_path / "state.sqlite3")
    client = WpsClient(
        lambda: _UnusedStream(),
        store,
        my_call="M0ABC",
        name="Tester",
        keepalive_interval=None,
        auto_reconnect=False,
    )

    async def _capture(obj: dict, *, _silence_reset: bool = True) -> None:
        sent.append(obj)

    # Bypass the connected-state guard in `_send` so we can drive
    # ``post()`` without a full handshake roundtrip.
    client._send = _capture  # type: ignore[assignment]
    return client, store, sent


def test_wps_client_post_emits_at_field_on_wire(tmp_path: Path) -> None:
    """``WpsClient.post(..., at_calls=[...])`` puts an ``at`` array on
    the outbound ``cp`` frame and persists it via ``upsert_post``."""
    client, store, sent = _build_client(tmp_path)
    ts = asyncio.run(client.post(5, "ping", at_calls=["m0xyz", "G7Bar"]))
    assert sent
    frame = sent[-1]
    assert frame["t"] == "cp"
    assert frame["cid"] == 5
    assert frame["p"] == "ping"
    # Callsigns are uppercased before they reach the wire.
    assert frame["at"] == ["M0XYZ", "G7BAR"]

    row = store.lookup_post(5, ts)
    assert row is not None
    assert json.loads(row["at_calls"]) == ["M0XYZ", "G7BAR"]
    store.close()


def test_wps_client_post_omits_at_field_when_none(tmp_path: Path) -> None:
    client, store, sent = _build_client(tmp_path)
    asyncio.run(client.post(5, "ping"))
    assert sent
    assert "at" not in sent[-1]
    store.close()


def test_wps_client_resend_post_re_emits_at(tmp_path: Path) -> None:
    """A ``cp`` resend re-emits the persisted ``at`` array so peers
    that missed the original delivery still see the mention list."""
    client, store, sent = _build_client(tmp_path)
    store.set_subscription(5, True)
    store.upsert_post(
        5,
        {
            "ts": 1_700_000_000_000,
            "fc": "M0ABC",
            "p": "hi",
            "at": ["G7BAR"],
        },
    )
    asyncio.run(client.resend_post(5, 1_700_000_000_000))
    assert sent
    frame = sent[-1]
    assert frame["t"] == "cp"
    assert frame["at"] == ["G7BAR"]
    store.close()
