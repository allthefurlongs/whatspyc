-- Local persistent state for whatspyc.
--
-- Mirrors what the web client keeps in IndexedDB. The most important
-- responsibility of this store is to provide accurate timestamps for the
-- type-`c` connect handshake so the server only sends deltas.
--
-- Timestamp units follow the protocol, which is asymmetric:
--   * DM `ts` is **seconds** since epoch (web client sends
--     `Math.round(Date.now()/1e3)` and renders with `ts*1e3`).
--   * Channel-post `ts` is **milliseconds** since epoch.
--   * DM `edts` and post `edts` are both **milliseconds** (the web
--     client uses `Math.round(Date.now())` / `Date.now()`).
-- `lm` in the connect record matches DM `ts` units (seconds). The store
-- mirrors the wire unit per column; display code uses an ms-normalising
-- helper so legacy mixed data still renders correctly.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,         -- WPS _id ("{ts}-{fc}")
    from_call    TEXT NOT NULL,
    to_call      TEXT NOT NULL,
    body         TEXT NOT NULL,
    ts           INTEGER NOT NULL,         -- seconds since epoch (DM convention)
    edit_ts      INTEGER,                  -- ms since epoch, NULL if unedited
    reply_id     TEXT,
    msg_status   INTEGER,
    -- local clock (ms) at first insert from the wire. NULL on rows we
    -- originated ourselves.
    received_ts  INTEGER,
    -- 1 if first observed via realtime `m`; 0 if via `mb` batch; NULL on
    -- rows we originated ourselves.
    realtime     INTEGER,
    -- ms since epoch when the server's `mr` ack arrived (local clock,
    -- since `mr` carries no dts). NULL if not yet acked / not our row.
    delivered_ts INTEGER
);

CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS messages_peer ON messages(from_call, to_call);

CREATE TABLE IF NOT EXISTS message_emojis (
    msg_id    TEXT NOT NULL,
    emoji     TEXT NOT NULL,
    -- The DM `mem` wire format carries no per-emoji authorship (just a
    -- set of emoji strings), so this is *locally inferred*: our own
    -- `react_message` writes my_call here; inbound `mem` deltas attribute
    -- new entries to the DM peer.
    callsign  TEXT NOT NULL DEFAULT '',
    emoji_ts  INTEGER NOT NULL,
    PRIMARY KEY (msg_id, emoji)
);

CREATE TABLE IF NOT EXISTS posts (
    channel_id   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    from_call    TEXT NOT NULL,
    body         TEXT NOT NULL,
    edit_ts      INTEGER,
    -- post-level emoji timestamp from the wire. The server bumps this
    -- whenever a reaction lands on the post; cpb posts carry the current
    -- value. Feeds the per-channel `le` floor in the connect record.
    ets          INTEGER,
    reply_ts     INTEGER,
    reply_from   TEXT,
    -- local clock (ms) at first insert from the wire. NULL on rows we
    -- originated ourselves.
    received_ts  INTEGER,
    -- 1 if first observed via realtime `cp`; 0 if via `cpb` batch; NULL
    -- on rows we originated ourselves.
    realtime     INTEGER,
    -- server-side `dts` from the `cpr` ack (or local clock fallback).
    -- NULL if not yet acked / not our row.
    delivered_ts INTEGER,
    -- 1 when the wire `g` flag was set on this post (first post after a
    -- paged-subscribe gap, per CHANNELS.md). Sticky once set. Drives the
    -- gap-aware `led`/`le` floor in the connect record so the server
    -- doesn't replay edits/reactions whose target posts fell into the
    -- gap and will never arrive.
    is_gap       INTEGER NOT NULL DEFAULT 0,
    -- JSON-encoded list of callsigns the post is addressed at, from the
    -- wire's `at` field (web-client @-mention picker). NULL when no
    -- mentions. The web client renders these as standalone styled tags
    -- *before* the post body — they never appear inside `body`. Set on
    -- the original `cp`; not editable (`cped` carries no `at`).
    at_calls     TEXT,
    PRIMARY KEY (channel_id, ts)
);

CREATE INDEX IF NOT EXISTS posts_channel_ts ON posts(channel_id, ts);

CREATE TABLE IF NOT EXISTS post_emojis (
    channel_id  INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    emoji       TEXT NOT NULL,
    callsign    TEXT NOT NULL,
    emoji_ts    INTEGER NOT NULL,
    PRIMARY KEY (channel_id, ts, emoji, callsign)
);

CREATE TABLE IF NOT EXISTS channels (
    cid           INTEGER PRIMARY KEY,
    subscribed    INTEGER NOT NULL DEFAULT 0,
    last_post     INTEGER NOT NULL DEFAULT 0,
    last_emoji    INTEGER NOT NULL DEFAULT 0,
    last_edit     INTEGER NOT NULL DEFAULT 0,
    -- Per-channel "read up to" cursor (ms, matches post.ts unit). The
    -- UI's unread count for a channel is the number of inbound posts
    -- with ts > last_read_ts. Bumped to MAX(post.ts) when the user
    -- activates the channel; survives restart so unread badges persist.
    last_read_ts  INTEGER NOT NULL DEFAULT 0
);

-- Per-DM-peer "read up to" cursor (seconds, matching DM ts unit on the
-- wire). Same role as channels.last_read_ts but for DMs — kept in its
-- own table because DM peers aren't otherwise modelled as a row (peers
-- are derived from messages.from_call / to_call).
CREATE TABLE IF NOT EXISTS dm_read (
    peer          TEXT PRIMARY KEY,
    last_read_ts  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hams (
    callsign  TEXT PRIMARY KEY,
    name      TEXT,
    last_ts   INTEGER NOT NULL DEFAULT 0
);
