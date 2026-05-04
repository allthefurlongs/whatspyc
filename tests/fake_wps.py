"""Tiny fake WPS server for offline smoke tests.

Speaks just enough of the protocol to be useful when a real WhatsPac host
isn't reachable. Pair it with ``--transport direct-tcp`` for an interactive
UI smoke test:

    # one terminal
    python tests/fake_wps.py --port 63001

    # another terminal — ALWAYS pass --state-dir to a throwaway path
    whatspyc --transport direct-tcp --host 127.0.0.1 --port 63001 \\
        --my-call N0CALL --name Tester \\
        --state-dir /tmp/whatspyc-fake

By default the server populates a small demo dataset on connect (see
``tests/fake_wps_seed.py``): three channels with posts, two DM
conversations, four online users, and a few known ham names. A short
"drip-feed" of live events follows the connect sequence so you can see
inbound messages arrive in real time. Pass ``--no-seed`` for the old
empty behaviour.

The handshake completes, ``mr`` / ``cpr`` acks come back for messages /
posts, ``cs`` echoes for subscribe / unsubscribe (with backfill for
seeded channels), and ``he`` returns name lookups. Edits, reactions and
keep-alives are accepted and silently dropped.

WARNING: this server is *stateless* across sessions — but the **client**
still writes everything it sees into its normal ``state_dir`` (default
``~/.local/share/whatspyc/state.sqlite3``). Always smoke-test with
``--state-dir /tmp/...`` so fake messages, fake ham rows, and bogus
channel cursors don't poison your real state and leak into the next
genuine connect handshake. ``rm -rf`` the throwaway dir between runs.

Usage::

    python tests/fake_wps.py --port 63001
    python tests/fake_wps.py --port 63001 --no-seed
"""

from __future__ import annotations

import argparse
import asyncio
import time

from whatspyc.wps import codec

try:
    # Imported as `tests.fake_wps` (pytest, integration tests).
    from tests.fake_wps_seed import SeedState, default_seed
except ImportError:
    # Run as a script: `python tests/fake_wps.py` puts tests/ on sys.path.
    from fake_wps_seed import SeedState, default_seed


_BATCH = 4  # messages per `mb`, posts per `cpb` (matches the real server)


async def _send_connect_seed(
    writer: asyncio.StreamWriter,
    *,
    my_call: str,
    connect: dict,
    seed: SeedState,
) -> None:
    """Drive a populated connect sequence: c-reply + mb/cpb/o/he batches.

    The client's connect record (``connect``) carries ``lm`` (last message
    timestamp, seconds) and ``cc[].lp`` (last post per channel,
    milliseconds). We deliver only entries newer than those, so a
    reconnect doesn't redeliver content the client already has.
    """
    last_msg_secs = int(connect.get("lm", 0))
    new_dms = seed.dms_after(my_call, last_msg_secs)

    cc_by_cid = {entry.get("cid"): entry for entry in connect.get("cc", [])}
    channel_deltas: dict[int, list[dict]] = {}
    for ch in seed.channels:
        last_post_ms = int(cc_by_cid.get(ch.cid, {}).get("lp", 0))
        posts = seed.channel_posts_after(ch.cid, last_post_ms)
        if posts:
            channel_deltas[ch.cid] = posts
    pc_total = sum(len(v) for v in channel_deltas.values())

    # 1) type-`c` server reply with new-message / new-post counts.
    writer.write(
        codec.encode({"t": "c", "mc": len(new_dms), "pc": pc_total, "v": 0.1})
    )

    # 2) DM batches (`mb`), 4 per batch.
    mt = len(new_dms)
    for i in range(0, mt, _BATCH):
        chunk = new_dms[i : i + _BATCH]
        writer.write(
            codec.encode(
                {"t": "mb", "md": {"mt": mt, "mc": i + len(chunk)}, "m": chunk}
            )
        )

    # 3) Per-channel: auto-subscribe ack, then `cpb` batches of posts. The
    #    `cs` makes the client mark the channel as subscribed in its store
    #    so subsequent connects send the right `cc[].lp` cursor.
    for cid, posts in channel_deltas.items():
        writer.write(
            codec.encode({"t": "cs", "s": 1, "cid": cid, "pc": len(posts)})
        )
        pt = len(posts)
        for i in range(0, pt, _BATCH):
            chunk = posts[i : i + _BATCH]
            writer.write(
                codec.encode(
                    {
                        "t": "cpb",
                        "cid": cid,
                        "m": {"pt": pt, "pc": i + len(chunk)},
                        "p": chunk,
                    }
                )
            )

    # 4) Online users.
    if seed.online:
        writer.write(codec.encode({"t": "o", "o": list(seed.online)}))

    # 5) Name lookups for everyone in the seed (covers DM peers + posters
    #    + online users so the UI can show real names instead of bare
    #    callsigns).
    he_payload = seed.he_payload()
    if he_payload:
        writer.write(codec.encode({"t": "he", "h": he_payload}))

    await writer.drain()


async def _live_drip(
    writer: asyncio.StreamWriter, *, my_call: str, seed: SeedState
) -> None:
    """Emit a few delayed events so the user sees real-time inbound traffic.

    Best-effort: if the writer is closed (client disconnected), silently
    bail out.
    """
    try:
        await asyncio.sleep(4.0)
        if writer.is_closing():
            return
        # Someone else "comes online".
        writer.write(codec.encode({"t": "uc", "c": "M0FOO"}))
        await writer.drain()

        await asyncio.sleep(3.0)
        if writer.is_closing():
            return
        # Inbound DM.
        ts = int(time.time() * 1000)
        writer.write(
            codec.encode(
                {
                    "t": "m",
                    "_id": f"{ts}-M0FOO",
                    "fc": "M0FOO",
                    "tc": my_call.upper(),
                    "m": "Just hopping on for a quick chat — got a sec?",
                    "ts": ts,
                }
            )
        )
        await writer.drain()

        await asyncio.sleep(5.0)
        if writer.is_closing():
            return
        # Inbound channel post.
        ts = int(time.time() * 1000)
        writer.write(
            codec.encode(
                {
                    "t": "cp",
                    "cid": 1,
                    "fc": "G7BAR",
                    "ts": ts,
                    "p": "Anyone got a recommendation for a 2m mobile?",
                }
            )
        )
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        return
    except Exception:
        return


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    seed: SeedState | None = None,
    *,
    live: bool = True,
) -> None:
    decoder = codec.FrameDecoder()
    drip_task: asyncio.Task | None = None
    callsign = ""

    # First line: callsign\r\n
    callsign_line = await reader.readuntil(b"\r\n")
    callsign = callsign_line.decode().strip()
    print(f"[fake_wps] callsign: {callsign}")

    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            for obj in decoder.feed(chunk):
                print(f"[fake_wps] recv: {obj}")
                t = obj.get("t")
                if t == "c":
                    if seed is not None:
                        await _send_connect_seed(
                            writer, my_call=callsign, connect=obj, seed=seed
                        )
                        if live and drip_task is None:
                            drip_task = asyncio.create_task(
                                _live_drip(writer, my_call=callsign, seed=seed)
                            )
                    else:
                        writer.write(
                            codec.encode({"t": "c", "mc": 0, "pc": 0, "v": 0.1})
                        )
                elif t == "m":
                    writer.write(codec.encode({"t": "mr", "_id": obj.get("_id", "")}))
                elif t == "cp":
                    writer.write(
                        codec.encode(
                            {"t": "cpr", "ts": obj["ts"], "dts": obj["ts"] + 1}
                        )
                    )
                elif t == "cs":
                    cid = obj.get("cid")
                    s = obj.get("s", 0)
                    if seed is not None and s == 1 and seed.channel(cid) is not None:
                        # Subscribing to a seeded channel: ack with the post
                        # count and backfill via `cpb`.
                        last_post_ms = int(obj.get("lcp", 0))
                        posts = seed.channel_posts_after(cid, last_post_ms)
                        writer.write(
                            codec.encode(
                                {"t": "cs", "s": 1, "cid": cid, "pc": len(posts)}
                            )
                        )
                        pt = len(posts)
                        for i in range(0, pt, _BATCH):
                            piece = posts[i : i + _BATCH]
                            writer.write(
                                codec.encode(
                                    {
                                        "t": "cpb",
                                        "cid": cid,
                                        "m": {"pt": pt, "pc": i + len(piece)},
                                        "p": piece,
                                    }
                                )
                            )
                    else:
                        writer.write(
                            codec.encode(
                                {"t": "cs", "s": s, "cid": cid, "pc": 0}
                            )
                        )
                elif t == "med":
                    # Real WPS acks an edit with the same `mr` it uses
                    # for the original send — sender clears its pending-
                    # edit timer on receipt.
                    writer.write(codec.encode({"t": "mr", "_id": obj.get("_id", "")}))
                elif t == "cped":
                    # `cped` ack is `cpr` (same as the original post).
                    writer.write(
                        codec.encode(
                            {"t": "cpr", "ts": obj["ts"], "dts": obj["ts"] + 1}
                        )
                    )
                elif t in ("k", "mem", "cpem", "cu"):
                    # Acceptable but no reply expected.
                    pass
                await writer.drain()
    finally:
        if drip_task is not None and not drip_task.done():
            drip_task.cancel()
            try:
                await drip_task
            except (asyncio.CancelledError, Exception):
                pass


async def serve(host: str, port: int, seed: SeedState | None, live: bool) -> None:
    async def _conn(reader, writer):
        await handle(reader, writer, seed, live=live)

    server = await asyncio.start_server(_conn, host, port)
    label = "with seed data" if seed is not None else "empty"
    print(f"[fake_wps] listening on {host}:{port} ({label})")
    async with server:
        await server.serve_forever()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=63001)
    p.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        help="Disable the demo dataset; behave like an empty server.",
    )
    p.add_argument(
        "--no-live",
        dest="live",
        action="store_false",
        help="Disable the post-connect drip-feed of live inbound events.",
    )
    p.set_defaults(seed=True, live=True)
    args = p.parse_args()
    seed = default_seed() if args.seed else None
    asyncio.run(serve(args.host, args.port, seed, args.live))


if __name__ == "__main__":
    main()
