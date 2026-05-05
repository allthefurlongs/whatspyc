# whatspyc — Python CLI client for WhatsPac

A terminal client for the [WhatsPac](http://whatspac.oarc.uk/) packet-radio
chat service. Speaks the WPS application protocol over an AX.25 link reached
either via XRouter / BPQ (using **RHP v2**) or directly via a KISS TNC.

What it does today:

- RHP v2 transports: WebSocket (`ws://host:8086/rhp` for Xrouter,
  `:8008` for BPQ) and raw TCP with a 2-byte length prefix.
- KISS transports: TCP (Direwolf / QtSoundModem-style) and serial, both
  driven by an in-process AX.25 v2.2 connected-mode L2 state machine
  (SABM/SABME/UA, I-frame N(S)/N(R) at modulo 8 or 128, RR/RNR/REJ/SREJ,
  T1/T3/N2). Optional digipeater paths, optional PID-0x08 segmentation,
  optional KISS ACKMODE (G8BPQ command 0x0C).
- A `direct-tcp` transport that talks straight to a raw WPS daemon (port
  63001 by default) — handy for offline UI testing against the in-tree
  `tests/fake_wps.py` server.
- AX.25 L2 (`pfam:"ax25"`) and L4 (`pfam:"netrom"`) selectable for RHP.
- **Multi-hop connect profiles**: each named profile carries a transport
  + endpoint + a `connect_sequence` of node-prompt commands (e.g.
  `C MB7NPW` → `Connected` → `C WPS` → `Connected`) that runs before the
  WPS handshake. Pick a profile from a startup picker, name one as the
  default, or build one ad-hoc from CLI flags.
- The full WPS application protocol — handshake, type-`c` connect,
  messages, posts, channel subscribe/unsubscribe, edits, emoji reactions,
  ham enquiry, keep-alive — with optional zlib/base64 compression on
  each frame.
- Local SQLite store of every message/post/edit/emoji seen, used to feed
  delta timestamps into the connect handshake (so reconnects don't pull
  the whole history).
- Two interactive front-ends: a `prompt_toolkit` line UI and a
  multi-pane `textual` TUI with per-target message panes, in-place
  edit/ack updates, scroll-back paging from the local store, a modal
  message-action menu, and a live online-users pane.
- Periodic keep-alives and (opt-in) automatic reconnect with exponential
  backoff — when enabled, the connect script auto-replays on every
  reconnect.

Avatars (`a` / `ar` / `ae`) and push notifications are explicitly out of
scope.

## Install

```bash
pip install -e .[dev]
```

## Quick start

Define one or more profiles in `~/.config/whatspyc/config.toml`, then run
`whatspyc` and pick one from the prompt:

```toml
my_call = "N0CALL"
default_profile = "via-mb7npw"

[[connect_profiles]]
name = "via-mb7npw"
transport = "rhp-ws"
host = "node.example.com"
engine = "xrouter"
remote = "MB7NPW"
connect_sequence = [
  { cmd = "C MB7NPW", val = "Connected to MB7NPW" },
  { cmd = "C WPS",    val = "Connected to WPS" },
]
```

The channel directory lives in its own file at
`~/.config/whatspyc/channels.toml`. The WPS protocol has no "list
channels" RPC, so the web client hardcodes its directory in the JS
bundle; whatspyc ships the same starting set as package data and writes
it to your config dir on first run. Edit, add, or remove entries
freely — whatspyc never overwrites the file once it exists. `/list`
in either UI shows subscribed channels first, then any directory entries
you're not subscribed to, then any saved DM threads. `/list ch` or
`/list dm` restricts the listing.

```bash
whatspyc                      # picker — default starred
whatspyc --profile via-mb7npw # specific profile, no picker
whatspyc --no-prompt          # default profile, unattended
```

Or skip the config file and build a one-shot ad-hoc connection from CLI
flags:

```bash
whatspyc \
  --transport rhp-ws --engine xrouter \
  --host node.example.com --my-call N0CALL --name Tester \
  --remote MB7NPW \
  --hop "C MB7NPW|Connected to MB7NPW" \
  --hop "C WPS|Connected to WPS"
```

While the connect attempt is in progress (hop chain, RHP open, type-`c`
settle) you can type `q` + Enter to cancel — the partial link is torn
down cleanly so you don't leave the entry node holding an orphaned
session. Once connected, `/quit` is the equivalent.

You'll land in a prompt. Slash commands:

| command | what |
| --- | --- |
| `/h [command]` | help. `/h` alone lists every slash command with a one-line summary; `/h <command>` shows detailed help (the leading slash is optional, so `/h ch` and `/h /ch` are equivalent). |
| `/dm CALL` | set the current target to a DM with `CALL` (callsign is uppercased) |
| `/ch CID` or `/ch NAME` | set the current target to a channel by id, or by name looked up in the channel directory (case-insensitive, leading `#` optional — `lounge` and `#lounge` both work). After history replay, if you're not subscribed, the UI prompts to subscribe — declining drops you back to whatever target you had before (or no target) so you don't get stranded in a channel you didn't commit to. |
| (plain text) | send to the current target. Posting to a channel you're not subscribed to is blocked at the client: WPS accepts the `cp` and broadcasts it to subscribers, but only relays new posts back to subscribers, so you'd never see replies — `/sub` first. |
| `/sub CID\|NAME [N]` | subscribe to a channel and pull `N` historic posts. The channel can be a numeric cid or a directory name (with or without `#`). Two-phase: send `cs`, await the server's ack (which carries the count of historic posts available), then either fetch `N` posts (when `N` was given), or prompt for the count with a sensible default. `N=0` subscribes realtime-only. The default for the prompt is `auto_backfill_post_count` (if configured) else 10, capped at the actual post count. |
| `/unsub CID\|NAME` | unsubscribe (cid or directory name, with or without `#`) |
| `/unpause CID\|NAME [N]` | for channels the server flagged via `pch` as having too many pending posts to deliver automatically. Sends `cu` to clear the server-side pause flag and download the last `N` posts (defaults to the count from the most recent `pch`). The channel can be a cid or a directory name (with or without `#`). |
| `/list` | list channels (subscribed first, then any `[[channels]]` directory entries you're not subscribed to) followed by saved DM threads. `/list ch` or `/list dm` restricts the listing to one or the other. |
| `/users` | list callsigns currently online — seeded by the type-`o` payload the server sends during connect, then kept in sync by `uc` / `ud` events. Cleared and re-seeded on every reconnect. |
| `/editdm LID text` | edit a DM you previously sent. `LID` is the local short id shown in the message log (a small integer — the SQLite rowid in the local store). Translated internally to the server's `_id` (`{ts}-{fc}`) before sending the `med` frame. The local row is updated immediately (the server doesn't echo edits back to the sender) and an edit-specific timeout is armed against the `mr` ack. The lid is session-local — it changes if you rebuild the state dir. Refused on rows you didn't send (surfaced as `[Cannot edit other users DMs]`). |
| `/editpost LID text` | edit a channel post you previously sent. `LID` is the local short id shown in the post log. Posts have no server-side identifier (they're keyed on `(cid, ts)`), so /editpost looks up that pair via the lid and sends the corresponding `cped` frame. Local row + edit-timeout machinery same as `/editdm`. Refused on rows you didn't send (surfaced as `[Cannot edit other users posts]`). |
| `/retrydm LID` | resend a DM you previously sent — `LID` is shown in the `[timeout] [dm:CALL] msg LID …` notice the client prints when no `mr` ack arrives within `delivery_timeout_s` (and is the same lid the verbose render uses for the `NOT DELIVERED` flip). **Edit-aware:** if the row has been edited (`edit_ts IS NOT NULL`), re-emits the latest `med` frame with the current body — that's almost always what the user actually wants. Otherwise re-emits the original `m`. Either form is server-idempotent (dedupes on `_id`). Resending restarts the per-row timeout window. Refused on rows you didn't send (surfaced as `[Cannot retry sending other users DMs]`). |
| `/retrypost LID` | resend a channel post — same edit-aware dispatch as `/retrydm`. If the post has been edited, re-emits `cped` with the current body; otherwise re-emits the original `cp`. Either form is idempotent on `(cid, ts)`. `LID` is shown in the `[timeout] [ch:CID #NAME] post LID …` notice. Refused on rows you didn't send (surfaced as `[Cannot retry sending other users posts]`). |
| `/react ID 1f44d` | emoji reaction (unicode codepoint hex). `ID` is the local short id from the log; dispatches on the current target — DM target sends `mem` against the message lid, channel target sends `cpem` against the post lid. |
| `/history [N]` | replay the last `N` historic messages/posts for the current target from the local store. Defaults to `history_backfill` if `N` is omitted. Output style follows the `verbose_history` session option (compact by default, verbose when set). The same backfill runs automatically each time you switch target. |
| `/vhistory [N]` | one-shot **verbose** replay of the last `N` items for the current target. Always renders the verbose form (local id, timestamp, delivery state for outbound items, real-time-receipt latency for inbound items) regardless of `verbose_history`. Does not change the session option. |
| `/set [NAME [VALUE]]` | view or change session-tunable options. `/set` lists every option with its current value and a one-line description; `/set NAME` shows just one; `/set NAME VALUE` updates it for the running session (does not persist — restart picks the config-file value back up). Values like `on`/`off`, `true`/`false`, `yes`/`no`, `1`/`0` are all accepted for booleans. Known options: `show_acks`, `show_edits`, `verbose_history`, `delivery_timeout_s`. Each option is also a top-level config key with the same name. |
| `/quit` | clean disconnect (drops the local AX.25 link; the rest of the chain follows) |

# Configuration reference

The config file lives at `$XDG_CONFIG_HOME/whatspyc/config.toml` (default
`~/.config/whatspyc/config.toml`). It is plain TOML; missing files and
missing keys are fine — defaults below apply.

The schema splits in two:

- **Global options** at the top level — your callsign, display name, UI
  choice, state-dir, the default profile.
- **One or more `[[connect_profiles]]`** — each a complete description
  of a single way to reach a WPS service: transport + endpoint + (for
  multi-hop paths) a `connect_sequence` of node-prompt commands.

Connection-specific keys (`transport`, `host`, `port`, `engine`,
`radio_port`, `ax_level`, `remote`, `rhp_auth_*`, all the `kiss_*` keys,
`digipeaters`, `ax25_modulo`, `ax25_segmentation`, `connect_sequence`)
**must** sit inside a `[[connect_profiles]]` block. Putting them at the
top level is rejected at config-load time.

## Global options

| config key | CLI flag | type | default | meaning |
| --- | --- | --- | --- | --- |
| `my_call` | `--my-call` | string | *(required)* | Your callsign — `BASE[-SSID]`, base must be 1–6 alphanumerics including at least one digit, SSID 0–15. The server strips the SSID before storing. **Required**, in either the file or via the flag. |
| `name` | `--name` | string | *(required)* | Display name in the type-`c` connect record. **Required**, in either the file or via the flag. |
| `ui` | `--ui` | string | `"line"` | `"line"` (prompt_toolkit) or `"tui"` (textual multi-pane — see [TUI key bindings](#tui-key-bindings) below). |
| `state_dir` | `--state-dir` | path | `$XDG_DATA_HOME/whatspyc` (i.e. `~/.local/share/whatspyc`) | Directory holding `state.sqlite3`. Created if missing. |
| `default_profile` | *(none)* | string \| null | `null` | Name of a configured profile to preselect in the picker / use under `--no-prompt`. Must match one of the `[[connect_profiles]]` names — typos are caught at config-load time. |
| `history_backfill` | *(none)* | int | `3` | How many historic messages (DM target) or posts (channel target) to replay from the local SQLite store each time you switch target. The same count is the default for `/history` when no explicit count is given. Set to `0` to disable the auto-replay. |
| `auto_backfill_post_count` | *(none)* | int \| null | `null` | Cap for paused (`pch`) channels at connect time and the default offered by `/sub`'s "how many historic posts?" prompt. When set: paused channels are auto-pulled at this cap; the `/sub` prompt's default reflects this value (capped at the actual `pc`). When unset/0: paused channels stay manual via `/unpause`, and `/sub` defaults to 10. |
| `auto_reconnect` | *(none)* | bool | `false` | When the link drops unexpectedly (EOF / read error), should the client transparently rebuild it? Off by default — the session ends and you re-run `whatspyc`. Turn on for unattended runs that need to ride out temporary node / transport hiccups. The `connect_sequence` replays automatically on every reconnect. See [Reconnect behaviour](#reconnect-behaviour). |
| `reconnect_max_retries` | *(none)* | int | `0` | Cap on consecutive reconnect attempts when `auto_reconnect` is on. `0` means retry forever. Anything > 0 gives up after that many failed attempts and prints `[link] giving up after N reconnect attempts`. Ignored when `auto_reconnect = false`. |
| `show_acks` | *(none)* | bool | `true` | Display the `[ack] [dm:CALL] msg LID …` / `[ack] [ch:N #name] post LID …` confirmation each time the server acknowledges a delivered DM (`mr`) or post (`cpr`). The `LID` is the local row id (same handle `/retrydm` / `/retrypost` take). Toggleable per session via `/set show_acks on|off` — useful confirmation on a slow link, noisy on a fast one. Note this only suppresses the *positive* ack line; the `[timeout] …` notice (see `delivery_timeout_s`) still prints when an ack fails to arrive in time. |
| `show_edits` | *(none)* | bool | `true` | Render an `[EDITED]` line in the message log when a real-time edit lands. Format mirrors a normal message line: `5 #lounge> [2026-05-03 18:39:20] <Matt, 2E0HKD>: [EDITED] new body` for posts, `dm M0FOO> [ts] <Name, CALL>: [EDITED] new body` for DMs. The timestamp is the edit's `edts`, not the original send time. Off → real-time `med` / `cped` frames still update the local store silently (so `/history` shows the new body) but no log line appears. Connect-batch edits (`medb` / `cpedb`) always update silently regardless of this setting — they're catch-up, not "live". Toggleable per session via `/set show_edits on|off`. |
| `verbose_history` | *(none)* | bool | `false` | Default rendering style for `/history`, target-switch backfill, and live arrivals. Compact form: `100 #lounge> [ts] <Bob, M6HKD>: msg`. Verbose form: `100 #lounge> ID: 71 - [ts] - Received real-time in 7s - <Bob, M6HKD>: msg` (inbound realtime), and `Delivered to server in 23s` / `Delivering...` / `NOT DELIVERED` for outbound. Toggleable per session via `/set verbose_history on|off`. `/vhistory` is always verbose regardless. |
| `delivery_timeout_s` | *(none)* | int | `60` | Seconds before an outbound DM (`m`) / post (`cp`) is treated as unacknowledged. When a row hits this deadline without a matching `mr` / `cpr` ack, the client prints a one-line timeout notice — e.g. `[timeout] [ch:5 #lounge] post 6 at [2026-05-03 16:46:42]. To resend: /retrypost 6` (or `[dm:M6HKD] msg 12 ...` with `/retrydm 12`). The notice **always** prints regardless of `show_acks`, since "no ack received" is harder to notice than "ack received". The same threshold also drives the verbose render's `Delivering...` → `NOT DELIVERED` flip. Whatspyc-specific — the web client has no automatic timeout (its "resend" button is purely manual). Toggleable per session via `/set delivery_timeout_s N`. |
| `log_level` | `--log-level` | string | `WARNING` | Python logging level (`CRITICAL` / `ERROR` / `WARNING` / `INFO` / `DEBUG` / `NOTSET`, case-insensitive). Resolution order: `--log-level` > `log_level` config key > `WHATSPYC_LOG` env var > built-in `WARNING`. |
| `log_file` | `--log-file` | path \| null | `null` | Append log records to this file. Additive — the console sink (see `log_console`) is unaffected, so any combination of file + console + neither is valid. The parent directory is created if missing. CLI flag wins over the config key; no env var. |
| `log_console` | `--log-console` | string | `"auto"` | Where the console-shaped log sink writes. Independent of `log_file`. Values: `"auto"` → status pane in TUI, stderr in line UI; `"stderr"` → always stderr (corrupts the TUI surface — opt-in only); `"pane"` → status pane (TUI only; line UI is **rejected** at startup); `"off"` → no console sink, file only (or silent if `log_file` is unset). In pane mode, `WARNING` and below appear in the pane (yellow for warnings); `ERROR`+ auto-shows the pane if it's hidden. |

## Connect profiles

Each `[[connect_profiles]]` table is a complete connection definition.
The only required key is `name`; everything else has a sensible default.
The picker at startup lists every configured profile with the
`default_profile` starred — Enter accepts the default, or type a number
or the profile name to choose another.

### Profile fields

| field | type | default | meaning |
| --- | --- | --- | --- |
| `name` | string | *(required)* | Profile name. Referenced by `default_profile` and `--profile NAME`. |
| `transport` | string | `"rhp-ws"` | `"rhp-ws"`, `"rhp-tcp"`, `"direct-tcp"`, `"kiss-tcp"`, `"kiss-serial"`. |
| `host` | string | `"localhost"` | Hostname / IP for non-serial transports. |
| `port` | int \| null | *(engine default)* | TCP/WS port. Engine-driven defaults apply unless you set this explicitly — see [Engine defaults](#engine-defaults). For RHP transports the engine resolves it; for `direct-tcp` and `kiss-tcp` it falls back to the transport default. |
| `engine` | string | *(required for RHP)* | `"xrouter"`, `"bpq"`, or `"custom"`. **Required for `transport = "rhp-ws"` / `"rhp-tcp"`**, ignored for other transports. Drives the defaults for `port`, `radio_port`, `remote`, and the BPQ `SWITCH` connect step — see [Engine defaults](#engine-defaults). Use `"custom"` to opt out of all defaulting and configure every field manually. |
| `radio_port` | int \| null | *(engine default)* | Radio-port index sent in the RHP `OPEN` message. Defaults to `1` for both `xrouter` and `bpq`. Serialized as a JSON string (`"1"`) on the wire, matching the production web client. `engine = "custom"` does not default it; `null` drops the field from the open. Ignored for `kiss-*`. |
| `ax_level` | string | `"L2"` | RHP `pfam`: `"L2"` → `"ax25"` (raw AX.25 to the radio), `"L4"` → `"netrom"` (NET/ROM Layer 4). Ignored for `kiss-*`. |
| `remote` | string | *(engine default)* | The AX.25 link-layer destination callsign. `engine = "bpq"` defaults this to `"SWITCH"` (BPQ's node command interface). Other engines default to `"WPS"`. Common values: `WPS`, `WPSDEV`, `MB7NPW-9`, `WTSPAC`, `SWITCH`. |
| `connect_sequence` | array of `{cmd, val, timeout?}` | `[]` | Node-prompt commands run **before** the WPS handshake. Empty for direct-tcp or RHP routes that already land you at WPS. See [Connect script](#connect-script). |
| `rhp_auth_user` | string \| null | `null` | RHP login if the host node requires it. Sends an `AUTH` message before `OPEN`. |
| `rhp_auth_pass` | string \| null | `null` | RHP password. |
| `kiss_device` | string \| null | `null` | Serial path for `kiss-serial` (e.g. `/dev/ttyUSB0`). **Required** when `transport = "kiss-serial"`. |
| `kiss_baud` | int | `9600` | Serial line speed. Ignored for `kiss-tcp`. |
| `kiss_port` | int | `0` | KISS sub-port byte (0–15). Use this when a multi-port TNC exposes more than one radio over the same KISS link. |
| `kiss_ackmode` | bool | `false` | Enable the G8BPQ KISS ACKMODE extension (command `0x0C`). The TNC echoes a synthetic ACK once each frame is on-air, so the L2 starts T1 from real over-the-air time. |
| `digipeaters` | list of strings | `[]` | AX.25 digi path between you and `remote`. Each entry `BASE[-SSID]`. Same chain rides on every I/S/U frame, including the SABM/SABME. KISS transports only. |
| `ax25_modulo` | int | `8` | `8` or `128`. `128` opens with SABME (extended mode) and uses 7-bit N(S)/N(R) — bigger windows + SREJ. Auto-falls-back to SABM/modulo-8 if the peer FRMRs/DMs. KISS transports only. |
| `ax25_segmentation` | bool | `false` | Enable AX.25 §4.3.3.2 PID-0x08 segmentation. KISS transports only. |

Each of these profile fields also has a corresponding CLI flag
(`--transport`, `--host`, `--port`, `--engine`, `--radio-port`,
`--ax-level`, `--remote`, `--kiss-device`, `--kiss-baud`, `--kiss-port`,
`--kiss-ackmode`/`--no-kiss-ackmode`, `--digipeaters`, `--ax25-modulo`,
`--ax25-segmentation`/`--no-ax25-segmentation`) that lets you build an
**ad-hoc profile** at the command line without touching the config file.
RHP auth has no CLI flag — set those in the file only.

### Engine defaults

`engine` is required for `rhp-ws` / `rhp-tcp`. It picks a coherent set of
defaults for the related fields so a typical profile only needs `host` (and
maybe a `connect_sequence`). Anything you set explicitly in the profile
overrides the corresponding default — so `engine = "bpq"` with `port = 8080`
keeps your custom port AND gets all the other BPQ smarts.

| field | `xrouter` | `bpq` | `custom` |
| --- | --- | --- | --- |
| `port` (rhp-ws) | `8086` | `8008` | *(no default — set it yourself)* |
| `port` (rhp-tcp) | `9000` | `9000` | *(no default — set it yourself)* |
| `radio_port` | `1` | `1` | not defaulted |
| `remote` | `"WPS"` | `"SWITCH"` *(BPQ's node command interface)* | not defaulted |
| `connect_sequence` | not modified | a wait-only preamble (`cmd = ""`, `val = "Connected to RHP Server"`) is auto-prepended to consume BPQ's unprompted greeting. Skipped if your first step is already wait-only or already targets that banner. | not modified |

Use `engine = "custom"` to opt out of all defaulting — typically when you
need to talk to a non-standard host node and want to wire every field
yourself. With `custom` and `transport = "rhp-ws"` you must set `port`
explicitly.

For `direct-tcp` and `kiss-*` transports `engine` is irrelevant and is
ignored if set.

### Connect script

`connect_sequence` walks the user through the chain of node-level
commands that get you from the entry node to the WPS service. Each
entry is a `{cmd, val, timeout?}` table:

- `cmd` — line sent to the current node prompt (e.g. `"C MB7NPW"`).
  Sent with a `\r` terminator (NOT `\r\n`). The literal string is sent;
  no `C ` prefix is added or stripped for you. Set to `""` for a
  **wait-only** step that sends nothing and just waits for `val` — useful
  when the remote pushes a banner unprompted (this is how `engine = "bpq"`
  consumes the auto-greeted `Connected to RHP Server` line).
- `val` — case-sensitive substring to wait for in the inbound text
  before advancing (e.g. `"Connected to MB7NPW"` or just `"Connected"`).
  Match is across the *accumulated* buffer, so trailing prompt bytes
  consumed during one step remain visible to the next.
- `timeout` (optional) — seconds before giving up on this step. Default
  is no timeout: the runner waits as long as the link stays open, mirroring
  the web client. Set a positive value to bound a specific step.

The runner aborts on a case-insensitive match against `Failure`,
`Busy`, `*** ` (the bare error prefix), or `Network Error`. The
`val` check runs **first** each iteration, so `*** Connected to WPS`
correctly wins over the `*** ` error prefix.

Direct-tcp connections and RHP routes whose `remote` already lands you
on the WPS service in one shot need no script at all — leave
`connect_sequence` out of the profile (or set it to `[]`).

The script also runs unchanged on every auto-reconnect attempt; nothing
extra to wire up.

```toml
connect_sequence = [
  { cmd = "C MB7NPW",      val = "Connected to MB7NPW",      timeout = 30 },
  { cmd = "C WPS",         val = "Connected to WPS" },
]
```

While the chain plays out, whatspyc prints each step to the terminal so
you can see what's happening:

```
[connect] profile=via-mb7npw transport=rhp-ws host=node.example.com hops=2
[hop 1/2] > C MB7NPW
[hop 1/2] < Welcome to NODE7  (XRouter v3)
[hop 1/2] < NODE7:M0ABC} *** Connected to MB7NPW
[hop 1/2] = matched 'Connected to MB7NPW'
[hop 2/2] > C WPS
[hop 2/2] < *** Connected to WPS
[hop 2/2] = matched 'Connected to WPS'
[connect-seq] mc=… pc=… …
```

`>` is what whatspyc sent, `<` is text from the node, `=` is the val
match that ended the step.

### Disconnect on quit

There is intentionally no scripted teardown. Closing the local AX.25 link
to the entry node (which `/quit` does as part of `stream.close()`) drops
that hop, and the rest of the AX.25 / NET-ROM chain tears down by
ordinary protocol behaviour. Adding `B` / `BYE` per hop would be redundant
and slow the close path down for no benefit.

### Reconnect behaviour

When the link drops unexpectedly (TCP EOF, KISS read error, RHP
disconnect, …) the default is to print `[link] disconnected` and end
the active session. The cli then prints `Disconnected from WPS.` and
either re-shows the connection-profile picker (if that's how you
landed on this profile in the first place) or asks
`Reconnect (r), or Quit (q)?` (Enter accepts the default of quit).
Picking a profile / typing `r` reconnects from scratch through the
full hop chain. Set `auto_reconnect = true` (top-level config key) to
transparently rebuild the stream instead — the same fallback prompt
fires once `reconnect_max_retries` is exhausted.

When enabled, the client retries with **exponential backoff**: the
first attempt waits 2 s, then doubles after each failure (4 s, 8 s, …)
up to a 60 s cap. A successful handshake resets the clock. The
`connect_sequence` replays unchanged on every attempt, so multi-hop
profiles need no extra wiring.

`reconnect_max_retries` caps how many *consecutive* failures the loop
tolerates before giving up. `0` (the default) means retry forever —
useful for headless runs over RF where the next ping might be hours
away. A finite cap is friendlier when you want the client to exit
cleanly if the node is genuinely down:

```toml
auto_reconnect = true
reconnect_max_retries = 10   # ~10 attempts, ~10 minutes wall time at the 60s cap
```

The application-level silence guard (240 minutes since the last
user-initiated send, mirroring the web client's `re=240`) still
overrides reconnect — once the client decides you've walked away, the
link closes and reconnect is suppressed regardless of the setting
above.

## CLI flags

Profile selection:

| flag | effect |
| --- | --- |
| `--profile NAME` | Use a configured profile by name. Skips the picker. |
| `--no-prompt` | Skip the picker; use `default_profile` (errors if unset). Useful for unattended runs. |
| `--hop "cmd\|val"` (repeatable) | Add an ad-hoc hop step. Triggers ad-hoc mode together with the per-profile flags. |
| *(none of the above)* | If profiles exist, show the numbered picker with the default starred. If no profiles AND no `--transport`, error pointing at the config file. |

`--profile` is mutually exclusive with `--hop` and the per-profile
flags (`--transport`, `--host`, etc.). Mixing them raises a usage error.

When you run `whatspyc` with no flags and the config has profiles:

```
Available connect profiles:
  1. fake  direct
  2. via-mb7npw (default)  2-hop
Profile num (v for profile details, q to quit) [2]:
```

Press Enter for the default; type `1` / `fake` / `2` / `via-mb7npw` to
override; type `v` to re-display the list with transport details and
each hop on its own line; type `q` to quit without connecting.

## Environment variables

| name | meaning |
| --- | --- |
| `WHATSPYC_LOG` | Fallback log level when neither `--log-level` nor the `log_level` config key is set. |
| `WEBSOCKETS_MAX_LOG_SIZE` | Maximum length of a `websockets` DEBUG-level frame dump before it gets truncated (with `...` in the middle). The library defaults to 75 chars, which clips every WPS payload. `whatspyc` raises this to 16 MiB at import time so DEBUG dumps include the full JSON; explicit values you set in the environment win. |
| `XDG_CONFIG_HOME` | Overrides the location of `config.toml` (looks for `$XDG_CONFIG_HOME/whatspyc/config.toml` instead of `~/.config/...`). |
| `XDG_DATA_HOME` | Overrides the default `state_dir` parent (`$XDG_DATA_HOME/whatspyc`). |
| `WHATSPYC_INTEGRATION_HOST` | Enables the `tests/integration/test_remote_smoke.py` test against a live node, e.g. `host` or `host:8086`. The remote test is skipped when unset. |
| `WHATSPYC_INTEGRATION_TRANSPORT` / `_CALL` / `_RADIO_PORT` / `_REMOTE` | Optional knobs for the integration smoke test (defaults: `rhp-ws`, `TEST-1`, `1`, `WPS`). |

## TUI key bindings

The textual TUI (`--ui tui` or `ui = "tui"` in config) is a
multi-pane interface built on [Textual](https://textual.textualize.io/).

Layout:

```
┌─Header───────────────────────────────────────────┐
├Tabs───┬─Status pane (Ctrl+S, hidden by default)──┤
│Ch DM  ├───────────────────────────────────────────┤
├───────┤ Per-target message ListView              │
│ch list│  (arrow-key selectable, auto-loads older │
│/dm    │   on cursor-at-top, in-place updates on  │
│list   │   edit/ack)                              │
├───────┤                                           │
│Online │                                           │
│users  │                                           │
├───────┴───────────────────────────────────────────┤
│ Input (or active modal)                           │
└Footer─────────────────────────────────────────────┘
```

| Key | Action |
| --- | --- |
| `Tab` / `Shift+Tab` | Cycle focus: input → message list → tab strip → target list → online list |
| `Esc` | Return focus to the input box |
| `← / →` (in tab strip) | Switch between Channels and DMs |
| `↑ / ↓` (in a list) | Navigate items |
| `↑` at top of message list | Auto-load the next older page from the local store |
| `Enter` (in target list) | Pin target as the send target, focus input |
| `Enter` (in message list) | Open action menu — Edit / Resend / React (Edit & Resend disabled for messages you didn't send) |
| `Ctrl+H` | Modal help screen — key bindings + slash commands |
| `Ctrl+D` | Toggle detailed (verbose) render — live re-renders every mounted row. Note: `Ctrl+V` is reserved by most terminals for paste, so this is `Ctrl+D`. |
| `Ctrl+S` | Toggle the status pane — chronological log of acks, edits, and link events |
| `Ctrl+C` / `Ctrl+Q` | Quit |

Behaviour:

- **In-place updates.** When a `med` / `cped` edit arrives, the
  matching message in the centre pane is rewritten in place (no
  duplicate `[EDITED]` line). When an `mr` / `cpr` ack arrives for
  one of your sends, the row gets a `✓` (compact mode) or
  `Delivered in Xs` (verbose mode) suffix.
- **Status pane (Ctrl+S).** Hidden by default. When open, every ack
  and edit also lands in a chronological log at the top of the
  centre pane. The message-row tick is updated regardless of whether
  this pane is visible.
- **Unread counts.** Inbound DMs / posts for a non-active target
  bump a `(N)` counter in the left-pane list label and **don't**
  mount into the centre pane. Activating that target zeroes the
  counter and pages history in from the local store.
- **Scroll-back.** Cursor up at the top of the message list fetches
  the next older page from the local SQLite store and prepends it
  in chronological order. Stops when the store is exhausted.
- **Action menu.** Enter on a message you sent → Edit / Resend /
  React. Edit reuses the input box (it loads with the current body;
  submit fires `med` / `cped`). Resend re-emits the original frame
  (or the latest edit if the row has been edited). React prompts for
  an emoji.
- **Slash-command parity.** Every command available in the line UI
  (`/sub`, `/ch`, `/editdm`, `/retrydm`, `/react`, `/set`, `/history`,
  `/vhistory`, ...) works in the TUI too. `/h` lists them all.
  `/history` / `/vhistory` are repurposed in the TUI: instead of
  appending a replay to the centre pane (line-UI semantics), they
  flip `verbose_history` (compact / verbose) and refresh every
  mounted row in place — the same effect as `Ctrl+D`, but absolute
  rather than a toggle. Any `[N]` arg is silently ignored. The
  centre pane already shows history; use `↑` at the top of the
  message list to load older pages.
- **Edit feedback.** When you edit your own DM (`/editdm LID …`,
  Action menu → Edit) or post (`/editpost LID …`), the displayed row
  updates its body immediately and dims (the same `[dim]` styling
  outbound rows get before their first ack) until the server's
  `mr` / `cpr` ack lands — at which point it un-dims.

## Example config files

### Loopback to `fake_wps` for offline UI testing

```toml
my_call = "N0CALL"
default_profile = "fake"

[[connect_profiles]]
name = "fake"
transport = "direct-tcp"
host = "127.0.0.1"
port = 63001
# no connect_sequence — direct-tcp goes straight to WPS
```

### Multi-hop XRouter route over WebSocket

`engine = "xrouter"` defaults `port = 8086` and `radio_port = 1`. `remote`
is set to the L2 entry node, then the script hops onward to WPS.

```toml
my_call = "N0CALL"
default_profile = "via-mb7npw"

[[connect_profiles]]
name = "via-mb7npw"
transport = "rhp-ws"
host = "node.example.com"
engine = "xrouter"
remote = "MB7NPW"
connect_sequence = [
  { cmd = "C MB7NPW", val = "Connected to MB7NPW" },
  { cmd = "C WPS",    val = "Connected to WPS" },
]
```

### BPQ via SWITCH, hop into WPS

`engine = "bpq"` defaults `port = 8008`, `remote = "SWITCH"`, `radio_port = 1`,
and auto-prepends a wait-only preamble that consumes the
`Connected to RHP Server` greeting BPQ sends unprompted on connect. Your
`connect_sequence` just lists the onward node-level hops to WPS.

```toml
my_call = "N0CALL-3"
name    = "Tester"
default_profile = "bpq"

[[connect_profiles]]
name = "bpq"
transport = "rhp-ws"
host = "bpq.example.org"
engine = "bpq"
connect_sequence = [
  { cmd = "C MB7NPW-9", val = "Connected to MB7NPW-9" },
]
```

If your BPQ runs on a non-default WebSocket port, set it explicitly —
engine defaults still cover everything else:

```toml
[[connect_profiles]]
name = "bpq-custom-port"
transport = "rhp-ws"
host = "raspberrypi"
engine = "bpq"
port = 8080
connect_sequence = [
  { cmd = "C 4 !MB7NPW-9", val = "*** Connected" },
]
```

### KISS-TCP to Direwolf with the textual TUI and a digi path

```toml
my_call = "N0CALL"
ui      = "tui"
default_profile = "direwolf"

[[connect_profiles]]
name = "direwolf"
transport = "kiss-tcp"
host = "127.0.0.1"
port = 8001
remote = "MB7NPW-9"
digipeaters = ["RELAY1", "RELAY2-7"]
connect_sequence = [
  { cmd = "C MB7NPW-9", val = "Connected to MB7NPW-9" },
  { cmd = "C WPS",      val = "Connected to WPS" },
]
```

### KISS-serial to a hardware TNC

```toml
my_call = "N0CALL"
default_profile = "tnc"

[[connect_profiles]]
name = "tnc"
transport = "kiss-serial"
kiss_device = "/dev/ttyUSB0"
kiss_baud = 9600
remote = "MB7NPW-9"
```

### RHP over plain TCP, with auth

```toml
my_call = "N0CALL"
default_profile = "rhp-tcp"

[[connect_profiles]]
name = "rhp-tcp"
transport = "rhp-tcp"
host = "node.example.com"
engine = "xrouter"
remote = "MB7NPW"
rhp_auth_user = "matt"
rhp_auth_pass = "hunter2"
```

### Several profiles, default chosen at startup

```toml
my_call = "N0CALL"
default_profile = "via-mb7npw"

[[connect_profiles]]
name = "fake"
transport = "direct-tcp"
host = "127.0.0.1"
port = 63001

[[connect_profiles]]
name = "via-mb7npw"
transport = "rhp-ws"
host = "node.example.com"
engine = "xrouter"
remote = "MB7NPW"
connect_sequence = [
  { cmd = "C MB7NPW", val = "Connected to MB7NPW" },
  { cmd = "C WPS",    val = "Connected to WPS" },
]

[[connect_profiles]]
name = "tnc-emergency"
transport = "kiss-serial"
kiss_device = "/dev/ttyUSB0"
remote = "MB7NPW-9"
```

## Default-port reference

When `port` is unset on a profile (and not passed on the CLI), the
default depends on `transport` and `engine`:

| transport | engine | default port |
| --- | --- | --- |
| `rhp-ws` | `xrouter` | 8086 |
| `rhp-ws` | `bpq` | 8008 |
| `rhp-ws` | `custom` | none — set `port` explicitly |
| `rhp-tcp` | `xrouter` / `bpq` | 9000 |
| `rhp-tcp` | `custom` | none — set `port` explicitly |
| `direct-tcp` | *(n/a)* | 63001 (WPS native TCP port) |
| `kiss-tcp` | *(n/a)* | 8001 |
| `kiss-serial` | *(n/a)* | uses `kiss_device` instead of a port |

## State directory layout

`state_dir` defaults to `$XDG_DATA_HOME/whatspyc`, falling back to
`~/.local/share/whatspyc` when `XDG_DATA_HOME` is unset (per the
[XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/)).
Override via `state_dir` in `config.toml` or `--state-dir` on the CLI.

```
state.sqlite3   single SQLite DB; tables: meta, messages, message_emojis,
                posts, post_emojis, channels, hams.
```

Read on connect to populate the type-`c` delta timestamps; written as
messages, posts, edits, emoji reactions, channel subscriptions and ham
lookups arrive. Safe to delete for a clean reset — the next connect
re-syncs from the server.

On first open, the store performs a one-shot migration that rewrites
ms-magnitude DM `ts` values (and the matching `last_message` cursor)
back to the wire unit (seconds). Earlier whatspyc versions sent DMs
with `ts` in milliseconds, which inflated the local cursor enough that
the server's `ts > lm` filter then rejected every legitimate
seconds-based DM on later reconnects (the "0 new DMs" symptom). The
migration is a no-op on fresh databases and on databases already in the
right unit.

## Advanced (Python API only)

A few link- and protocol-level knobs aren't surfaced as CLI flags / config
keys — pass them when constructing `Ax25L2Stream` or `WpsClient` from
Python:

`Ax25L2Stream(...)` (in `whatspyc.transport.ax25_l2`):

| kwarg | default | meaning |
| --- | --- | --- |
| `t1` | `10.0` s | Acknowledgement timeout. Drives retry / poll. |
| `t3` | `300.0` s | Link-idle keepalive. Sends an RR P=1 to verify the peer. |
| `n2` | `10` | Maximum retry count before declaring the link broken. |
| `window` | `4` | I-frames in flight (max `7` modulo-8, max `127` modulo-128). |
| `paclen` | `256` | Maximum I-frame information field. Larger user writes are split (or PID-0x08 segmented when `segmentation=True`). |
| `connect_timeout` | `30.0` s | Overall SABM-to-UA wait at `open()`. |
| `digipeaters` | `[]` | List of digipeater callsigns (`"DIGI1"`, `"DIGI2-7"`, ...) inserted between source and destination on every frame. |
| `modulo` | `8` | `8` or `128`. Selects SABM vs SABME and the I/S-frame control-field width. Falls back to 8 on FRMR/DM in response to SABME. |
| `segmentation` | `False` | Enable AX.25 §4.3.3.2 PID-0x08 segmentation/reassembly. |
| `ackmode` | `False` | Defer T1 until KISS ACKMODE confirms each I-frame is on-air. Requires the lower (`KissTcpUI`/`KissSerialUI`) to be opened with `ackmode=True` too — `connect_stream(...)` does this for you. |

`WpsClient(...)` (in `whatspyc.wps.client`):

| kwarg | default | meaning |
| --- | --- | --- |
| `keepalive_interval` | `540.0` s | How often to send `{"t":"k"}`. Default 9 minutes matches the web client's `keepAliveIntervalMinutes`. `None` disables. |
| `keepalive_max_minutes` | `240` | Application-level silence guard: if the time since the last user-initiated send exceeds this many minutes the link is closed and auto-reconnect is suppressed (the user has clearly walked away). Mirrors the web client's hardcoded `re=240`. Keep-alives don't reset the clock; only real outbound traffic does. `None` disables. |
| `auto_reconnect` | `False` | Rebuild the stream after a link-loss event. Off by default — also surfaced as the top-level `auto_reconnect` config key (see [Reconnect behaviour](#reconnect-behaviour)). |
| `reconnect_initial_backoff` | `2.0` s | First reconnect delay. Doubles after each failure. |
| `reconnect_max_backoff` | `60.0` s | Cap for exponential backoff. |
| `reconnect_max_retries` | `0` | Cap on consecutive reconnect attempts. `0` means retry forever; positive values emit a `_reconnect_giveup` event after that many failures. Also surfaced as the top-level `reconnect_max_retries` config key. |
| `connect_script` | `[]` | List of `HopStep(cmd, val, timeout?)` to run between `stream.open()` and the WPS callsign-line send. Replays automatically on every reconnect. |

## Offline UI testing with `fake_wps`

`tests/fake_wps.py` is a tiny standalone server that speaks just enough of
the WPS protocol for the UI to work end-to-end. Pair it with the
`direct-tcp` transport (no `connect_sequence` needed — fake_wps speaks
WPS directly) for a no-radio, no-RHP loopback you can drive interactively
in either UI:

```bash
# terminal 1 — start the fake server (listens on 127.0.0.1:63001)
python tests/fake_wps.py --port 63001
```

Then either define a profile in your config…

```toml
default_profile = "fake"

[[connect_profiles]]
name = "fake"
transport = "direct-tcp"
host = "127.0.0.1"
port = 63001
```

```bash
# terminal 2 — connect with the line UI (default)
whatspyc --my-call N0CALL --name Tester --state-dir /tmp/whatspyc-fake

# …or with the textual TUI
whatspyc --my-call N0CALL --name Tester --ui tui --state-dir /tmp/whatspyc-fake
```

…or skip the config and pass everything on the CLI:

```bash
whatspyc \
  --transport direct-tcp \
  --host 127.0.0.1 --port 63001 \
  --my-call N0CALL --name Tester \
  --state-dir /tmp/whatspyc-fake
```

> **Always pass `--state-dir` to a throwaway path when smoke-testing.**
> `fake_wps` keeps its demo dataset in memory only — nothing is written
> to disk on the server side — but the **client** still writes
> everything it sees into its normal `state_dir` (default
> `~/.local/share/whatspyc/state.sqlite3`). Without `--state-dir`, your
> real state DB will accumulate fake messages, fake ham rows, and bogus
> channel cursors that then get fed back into the next real connect
> handshake. `rm -rf /tmp/whatspyc-fake` between runs for a clean slate.

### Seeded demo dataset (default)

By default the fake server populates an in-memory dataset on connect so
the UI has something to render straight away — defined in
`tests/fake_wps_seed.py` and easy to extend:

- **Three channels with posts**: `cid=1` (general), `cid=2` (packet),
  `cid=6` (lounge). The server auto-subscribes you (sends a `cs` ack
  per channel) and follows up with `cpb` batches, so all three appear
  in the TUI's target list immediately.
- **Two DM conversations**: with `M0FOO` (Mike) and `G7BAR` (Sarah),
  carrying messages in both directions.
- **Four "online" users**: `M0FOO`, `G7BAR`, `2E0BAZ`, `M7QRP` — sent
  as the connect-sequence `o` payload.
- **Ham name lookups**: a single `he` payload covers every callsign in
  the dataset, so names display in place of bare callsigns.
- **A short live drip-feed** after the connect settles: a `uc` ("user
  came online"), then an inbound DM, then a channel post — to exercise
  real-time rendering without you having to send anything.

The server respects the `lm` and `cc[].lp` cursors the client sends,
so reconnecting against the same `state_dir` won't redeliver content
the client already has. Restarting `fake_wps` resets its base
timestamp, so the same seeded items will look "newer" again on the
next session.

Subscribing to a seeded channel from the client (`/sub 1`) also
triggers a fresh `cpb` backfill, so you can poke at the subscribe path
explicitly.

| flag | effect |
| --- | --- |
| *(default)* | seeded dataset + live drip-feed |
| `--no-seed` | empty server: type-`c` reply has `mc=0`, `pc=0`; only acks and `he` synthetics, like the original behaviour |
| `--no-live` | seeded dataset, but no post-connect drip-feed |

### What the fake handles

| client sends | fake replies |
| --- | --- |
| `<CALL>\r\n` then type-`c` | type-`c` with `mc`/`pc`; `mb`, `cs`+`cpb` per channel, `o`, `he` (or `mc=0`/`pc=0` only with `--no-seed`) |
| `m` (DM) | `mr` ack |
| `cp` (channel post) | `cpr` ack with `dts` |
| `cs` (subscribe) | `cs` ack + `cpb` backfill if seeded; otherwise `cs` with `pc=0` |
| `cs` (unsubscribe) | `cs` ack with `pc=0` |
| `k`, `med`, `mem`, `cpem`, `cu`, `cped` | accepted, no reply (won't error) |

So you can exercise `/dm`, `/ch`, plain text, `/sub`, `/unsub`,
`/editdm`, `/editpost`, `/react`, `/quit` against the same in-process
state machine the real client uses, without needing RHP or a TNC.

## Multi-hop UI testing with `fake_node`

`tests/fake_node.py` is a runnable companion to `fake_wps`. It pretends
to be a packet node: emits a banner, presents a prompt, accepts
`C <CALL>` commands, and on the final hop splices the connection through
to a backing WPS port. Pair it with a `direct-tcp` profile whose
`connect_sequence` walks the same chain and you can watch the hop-script
runner play out for real, ending in a normal WPS session against the
seeded `fake_wps` dataset.

> **The number of hops in your `connect_sequence` must match `fake_node
> --hops N`.** If they're out of sync, your script declares success after
> reaching an *intermediate* prompt, then sends the WPS callsign-line
> straight into the still-active node — the node treats it as garbage,
> replies `*** Failure - unknown command\r`, and whatspyc surfaces:
>
> ```
> [error] RuntimeError("connect_sequence likely incomplete: server's
>   first reply isn't a WPS frame, it's plain text — '*** Failure -
>   unknown command'. Check that every hop in the script matches the
>   node's prompts.")
> ```
>
> The fix is to line up your `connect_sequence` with `--hops`: one
> entry per hop, every entry's `val` matching what each node-stage
> emits.

### Single-hop demo

```bash
# Terminal A — fake WPS daemon
python tests/fake_wps.py --port 63001

# Terminal B — fake node (1-hop), splicing into the WPS port above
python tests/fake_node.py --port 7000 --wps-port 63001
```

```toml
# ~/.config/whatspyc/config.toml
my_call = "N0CALL"
default_profile = "via-fakenode"

[[connect_profiles]]
name = "via-fakenode"
transport = "direct-tcp"
host = "127.0.0.1"
port = 7000
connect_sequence = [
  { cmd = "C WPS", val = "Connected to WPS" },
]
```

```bash
# Terminal C — drive the client
whatspyc --no-prompt --my-call N0CALL --state-dir /tmp/whatspyc-fake
```

You'll see the chain print as it plays out, then the seeded WPS UI
takes over:

```
[connect] profile=via-fakenode transport=direct-tcp host=127.0.0.1 hops=1
[hop 1/1] > C WPS
[hop 1/1] < Welcome to NODE1  (fake-node)
[hop 1/1] < NODE1:M0ABC} *** Connected to WPS
[hop 1/1] = matched 'Connected to WPS'
Connected. /h for help, /list to view channels
[connect] new messages: 7, new posts: 12, version: 0.1
[DM*] M0FOO -> M0ABC: Hey, did you see my last QRZ?
…
```

### Multi-hop demo

Pass `--hops 2` to `fake_node` and it presents two layered prompts —
the first expects `C MB7NPW`, the second expects `C WPS`:

```bash
python tests/fake_node.py --port 7000 --wps-port 63001 --hops 2
```

```toml
[[connect_profiles]]
name = "via-mb7npw-fake"
transport = "direct-tcp"
host = "127.0.0.1"
port = 7000
connect_sequence = [
  { cmd = "C MB7NPW", val = "Connected to MB7NPW" },
  { cmd = "C WPS",    val = "Connected to WPS" },
]
```

Output:

```
[hop 1/2] > C MB7NPW
[hop 1/2] < Welcome to NODE1  (fake-node)
[hop 1/2] < NODE1:M0ABC} *** Connected to MB7NPW
[hop 1/2] = matched 'Connected to MB7NPW'
[hop 2/2] > C WPS
[hop 2/2] < MB7NPW:M0ABC} *** Connected to WPS
[hop 2/2] = matched 'Connected to WPS'
…
```

`fake_node` itself logs each command it sees on stderr, so you can
correlate the two sides if the chain doesn't behave the way you
expected:

```
[fake-node] listening on ('127.0.0.1', 7000); backing WPS at 127.0.0.1:63001; hops=2
[fake-node] connection from ('127.0.0.1', 45309) opened
[fake-node] NODE1 got 'C MB7NPW'
[fake-node] MB7NPW got 'C WPS'
[fake-node] splicing to 127.0.0.1:63001 ('WPS')
```

### Exercising the error-abort path

To see what happens when a hop fails, send a bogus first command — the
runner aborts on `*** Failure …`:

```bash
whatspyc --transport direct-tcp --host 127.0.0.1 --port 7000 \
         --my-call N0CALL --state-dir /tmp/whatspyc-fake \
         --hop "BOGUS|Connected"
```

```
[hop 1/1] > BOGUS
HopScriptError: node returned error token 'FAILURE' while waiting for
'Connected': …'unknown command\r'…
```

> **Quick reset between runs.** Both fake servers are stateless on the
> server side, but the client still writes everything it sees into its
> `state_dir`. Use a throwaway path (e.g. `--state-dir
> /tmp/whatspyc-fake`) and `rm -rf` it between runs to keep your real
> state DB clean.

## Tests

```bash
pytest
```

`tests/integration/` holds end-to-end tests: a KISS-TCP+L2 loopback, a
fake-WPS direct-TCP smoke, and a fake-node-prompt + fake-WPS hop-script
test (all always run); a remote-node smoke test gated on
`WHATSPYC_INTEGRATION_HOST`.

