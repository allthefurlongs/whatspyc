"""Seed data for the offline fake WPS server.

``tests/fake_wps.py`` uses these helpers to populate an in-memory "test
database" so the client UI shows a few demo channels with posts, DMs in
both directions, and a list of online users on connect — without needing a
real WhatsPac host.

Times are computed relative to the fake server's boot time so each run
looks "recent". The seed lives only for the lifetime of one fake_wps
process; nothing on disk.

Usage from the fake server::

    seed = default_seed()
    await asyncio.start_server(lambda r, w: handle(r, w, seed=seed), ...)

To extend the demo, edit :func:`default_seed` or build a custom
:class:`SeedState` of your own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SeedPost:
    from_call: str
    body: str
    age_seconds: int  # how long ago this post was authored, vs seed boot

    def ts_ms(self, base_ms: int) -> int:
        return base_ms - self.age_seconds * 1000


@dataclass
class SeedChannel:
    cid: int
    name: str
    posts: list[SeedPost] = field(default_factory=list)


@dataclass
class SeedDM:
    """Direct messages with one peer.

    Each entry in ``messages`` is ``(direction, body, age_seconds)`` where
    ``direction`` is ``"in"`` (peer→me) or ``"out"`` (me→peer).
    """

    peer_call: str
    peer_name: str
    messages: list[tuple[str, str, int]] = field(default_factory=list)


@dataclass
class SeedState:
    channels: list[SeedChannel]
    dms: list[SeedDM]
    online: list[str]
    hams: dict[str, str]  # callsign -> name (for `he` lookups)
    base_ts_ms: int

    def channel(self, cid: int) -> SeedChannel | None:
        for c in self.channels:
            if c.cid == cid:
                return c
        return None

    def channel_posts_after(self, cid: int, after_ms: int) -> list[dict]:
        """Wire-format posts for ``cid`` newer than ``after_ms`` (ms epoch)."""
        ch = self.channel(cid)
        if ch is None:
            return []
        out: list[dict] = []
        for p in ch.posts:
            ts = p.ts_ms(self.base_ts_ms)
            if ts > after_ms:
                out.append({"fc": p.from_call.upper(), "ts": ts, "p": p.body})
        out.sort(key=lambda x: x["ts"])
        return out

    def dms_after(self, my_call: str, after_seconds: int) -> list[dict]:
        """Wire-format DMs to/from ``my_call`` newer than ``after_seconds``.

        ``after_seconds`` is the connect record's ``lm`` field (epoch
        seconds) — DM timestamps on the wire are milliseconds, so callers
        need only feed in the value the client just sent.
        """
        my_call = my_call.upper()
        after_ms = after_seconds * 1000
        out: list[dict] = []
        for dm in self.dms:
            for direction, body, age in dm.messages:
                ts = self.base_ts_ms - age * 1000
                if ts <= after_ms:
                    continue
                if direction == "in":
                    fc, tc = dm.peer_call.upper(), my_call
                elif direction == "out":
                    fc, tc = my_call, dm.peer_call.upper()
                else:
                    continue
                out.append(
                    {
                        "_id": f"{ts}-{fc}",
                        "fc": fc,
                        "tc": tc,
                        "m": body,
                        "ts": ts,
                    }
                )
        out.sort(key=lambda m: m["ts"])
        return out

    def he_payload(self, callsigns: list[str] | None = None) -> list[dict]:
        """Build the ``he.h`` array. With no argument, return every known ham."""
        if callsigns is None:
            wanted = list(self.hams.keys())
        else:
            wanted = [c.upper() for c in callsigns]
        return [
            {"c": c, "n": self.hams.get(c, f"Fake {c}"), "ts": self.base_ts_ms}
            for c in wanted
        ]


def default_seed() -> SeedState:
    """Three channels, two DM peers, four online users — a reasonable demo set."""
    base_ms = int(time.time() * 1000)
    channels = [
        SeedChannel(
            cid=1,
            name="general",
            posts=[
                SeedPost("M0FOO", "Morning all, propagation looks good today.", 7200),
                SeedPost("G7BAR", "FT8 from EA8 booming on 20m.", 5400),
                SeedPost("M0FOO", "Anyone seeing the same?", 5300),
                SeedPost("2E0BAZ", "Yep, S9 here in IO91.", 4800),
                SeedPost("M0FOO", "73 all.", 60),
            ],
        ),
        SeedChannel(
            cid=2,
            name="packet",
            posts=[
                SeedPost("G7BAR", "Tried out the new Direwolf build last night.", 10800),
                SeedPost("M7QRP", "Any luck on 1200 baud?", 9000),
                SeedPost("G7BAR", "Yeah, solid copy via GB7CIP digi.", 8700),
                SeedPost("MM0XYZ", "Nice — sked tonight 8pm 144.800?", 1800),
            ],
        ),
        SeedChannel(
            cid=6,
            name="lounge",
            posts=[
                SeedPost("M7QRP", "Coffee on. Anyone else QRV?", 600),
                SeedPost("2E0BAZ", "I'm here, brew incoming.", 540),
                SeedPost("M7QRP", "Excellent.", 480),
            ],
        ),
    ]
    dms = [
        SeedDM(
            peer_call="M0FOO",
            peer_name="Mike",
            messages=[
                ("in", "Hey, did you see my last QRZ?", 14400),
                ("out", "Yep — I'll log it tonight.", 14100),
                ("in", "Cheers. Also, fancy a sked Saturday on 40m?", 3600),
                ("in", "Around 1830z?", 3590),
            ],
        ),
        SeedDM(
            peer_call="G7BAR",
            peer_name="Sarah",
            messages=[
                ("in", "Pi build worked first time, thanks.", 7200),
                ("out", "Great — got it talking to the node?", 7100),
                ("in", "Yep, all green over RHP.", 7000),
            ],
        ),
    ]
    return SeedState(
        channels=channels,
        dms=dms,
        online=["M0FOO", "G7BAR", "2E0BAZ", "M7QRP"],
        hams={
            "M0FOO": "Mike",
            "G7BAR": "Sarah",
            "2E0BAZ": "Dave",
            "M7QRP": "Lou",
            "MM0XYZ": "Alex",
        },
        base_ts_ms=base_ms,
    )
