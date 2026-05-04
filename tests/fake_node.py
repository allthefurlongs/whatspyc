"""Standalone fake node-prompt server for smoke-testing connect_sequence.

Sits in front of ``fake_wps.py`` and pretends to be a packet node: emits
a banner, presents a prompt, accepts ``C <CALL>`` commands, and on the
final hop splices the connection through to a backing WPS port. Pair it
with the ``direct-tcp`` transport in whatspyc and a ``connect_sequence``
that walks the same chain to see the hop-script runner play out for real.

Defaults to a single-hop dialogue (one prompt → one ``C WPS`` →
splice). Pass ``--hops 2`` for a two-layer chain (NODE1 → ``C MB7NPW`` →
MB7NPW prompt → ``C WPS`` → splice).

Example session::

    # Terminal A — fake WPS daemon
    python tests/fake_wps.py --port 63001

    # Terminal B — fake node, 2-hop, splicing into the WPS port above
    python tests/fake_node.py --port 7000 --wps-port 63001 --hops 2

    # Terminal C — drive the client (config snippet shown in README)
    whatspyc --no-prompt --my-call N0CALL --state-dir /tmp/whatspyc-fake

The node prints each command it receives on stderr (e.g.
``[fake-node] NODE1 got 'C MB7NPW'``) so you can correlate it with
whatspyc's ``[hop i/n] > …`` lines.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def _splice(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(4096)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def _read_cr_terminated_line(reader: asyncio.StreamReader) -> str | None:
    """Read up to (and including) a single ``\\r``. The hop-script runner
    sends commands that way; bare ``\\n`` is tolerated too in case someone
    is poking at the port with a regular telnet."""
    line = bytearray()
    while True:
        ch = await reader.read(1)
        if not ch:
            return None
        line.extend(ch)
        if ch in (b"\r", b"\n"):
            return bytes(line).rstrip(b"\r\n").decode("latin-1").strip()


def _make_handler(wps_host: str, wps_port: int, hops: int):
    # Layered node names. The last entry is the prompt that triggers the
    # splice into fake_wps.
    chain = (["NODE1", "MB7NPW", "K4XYZ"])[:hops]

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        print(f"[fake-node] connection from {peer} opened", file=sys.stderr, flush=True)
        try:
            writer.write(
                f"\r\nWelcome to {chain[0]}  (fake-node)\r\n".encode("latin-1")
            )
            for i, node_name in enumerate(chain):
                writer.write(f"{node_name}:M0ABC}} ".encode("latin-1"))
                await writer.drain()
                line = await _read_cr_terminated_line(reader)
                if line is None:
                    return
                print(f"[fake-node] {node_name} got {line!r}", file=sys.stderr, flush=True)
                if not line.upper().startswith("C "):
                    writer.write(b"*** Failure - unknown command\r")
                    await writer.drain()
                    return
                target = line.split(None, 1)[1].strip()
                if i < len(chain) - 1:
                    # Intermediate hop — emit a Connected line + drop into
                    # the next node's prompt.
                    writer.write(
                        f"*** Connected to {target}\r\n".encode("latin-1")
                    )
                    await writer.drain()
                    continue
                # Final hop — emit Connected and splice through to fake_wps.
                writer.write(f"*** Connected to {target}\r".encode("latin-1"))
                await writer.drain()
                print(
                    f"[fake-node] splicing to {wps_host}:{wps_port} "
                    f"({target!r})",
                    file=sys.stderr,
                    flush=True,
                )
                wps_reader, wps_writer = await asyncio.open_connection(
                    wps_host, wps_port
                )
                c2w = asyncio.create_task(_splice(reader, wps_writer))
                w2c = asyncio.create_task(_splice(wps_reader, writer))
                try:
                    await asyncio.gather(c2w, w2c, return_exceptions=True)
                finally:
                    for w in (writer, wps_writer):
                        try:
                            w.close()
                        except Exception:
                            pass
                return
        finally:
            try:
                writer.close()
            except Exception:
                pass
            print(
                f"[fake-node] connection from {peer} closed",
                file=sys.stderr,
                flush=True,
            )

    return handle


async def _serve(args: argparse.Namespace) -> None:
    handler = _make_handler(args.wps_host, args.wps_port, args.hops)
    server = await asyncio.start_server(handler, args.host, args.port)
    sock = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(
        f"[fake-node] listening on {sock}; backing WPS at "
        f"{args.wps_host}:{args.wps_port}; hops={args.hops}",
        file=sys.stderr,
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--wps-host", default="127.0.0.1")
    parser.add_argument("--wps-port", type=int, default=63001)
    parser.add_argument(
        "--hops",
        type=int,
        default=1,
        choices=[1, 2, 3],
        help="Number of layered node prompts before splicing to fake_wps. "
        "Default 1 (one prompt → one C WPS → splice). 2 simulates "
        "NODE1 → C MB7NPW → MB7NPW prompt → C WPS → splice.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
