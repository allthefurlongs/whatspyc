"""Typed records for every documented WPS message type plus ``cu``.

Each dataclass models one message *body* (without the ``t`` discriminator,
which is added on encode and consumed on decode). The map ``KEY_MAP``
translates between short wire keys (``fc``) and python attribute names
(``from_call``).

Validation is deliberately thin — we trust the server. Decode only checks
the discriminator and route to the right dataclass; missing optional fields
default to None.

Server-only fields (``ms``, ``lts``, ``dts``) appear on inbound messages
only; outbound encoding never includes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar


# Attribute name -> wire key. Inverse used at parse time.
KEY_MAP: dict[str, str] = {
    "type_": "t",
    "msg_id": "_id",
    "from_call": "fc",
    "to_call": "tc",
    "callsign": "c",
    "name": "n",
    "version": "v",
    "message": "m",
    "post": "p",
    "timestamp": "ts",
    "logged_ts": "lts",
    "delivery_ts": "dts",
    "edit_ts": "edts",
    "emoji_ts": "ets",
    "msg_status": "ms",
    "channel_id": "cid",
    "post_count": "pc",
    "msg_count": "mc",
    "welcome": "w",
    "subscribe": "s",
    "action": "a",
    "emoji": "e",
    "edits": "ed",
    "channels": "cc",
    "channel_headers": "ch",
    "posts_total": "pt",
    "last_message": "lm",
    "last_emoji": "le",
    "last_edit": "led",
    "last_post": "lp",
    "last_ham_ts": "lhts",
    "last_avatar_ts": "lats",
    "last_channel_post": "lcp",
    "last_seen": "ls",
    "registered": "r",
    "online": "o",
    "users": "u",
    "hams": "h",
    "messages": "m",
    "posts": "p",
    "stats": "s",
    "meta": "md",
    "avatar": "a",
    "avatar_count": "ac",
    "count_only": "co",
    "reply_id": "r",
    "reply_ts": "rts",
    "reply_from": "rfc",
    "gap": "g",
    "edited_flag": "ed",
    "enabled": "e",
    "start_time": "st",
    "messages_field": "m",  # only used for `mb.m`
}

# When a single attribute is mapped to a wire key that's also used on a
# different message, we'd lose round-trip-ness. We solve this per-message by
# registering only the keys each message actually uses.


def _from_dict(cls, d: dict[str, Any]):
    """Instantiate a message dataclass from its wire dict."""
    init = {}
    for f in fields(cls):
        wire = cls._WIRE_KEYS.get(f.name, KEY_MAP.get(f.name, f.name))
        if wire in d:
            init[f.name] = d[wire]
    return cls(**init)


def _to_dict(self) -> dict[str, Any]:
    out: dict[str, Any] = {"t": self.TYPE}
    for f in fields(self):
        v = getattr(self, f.name)
        if v is None:
            continue
        if f.name in self._SERVER_ONLY:
            continue
        wire = self._WIRE_KEYS.get(f.name, KEY_MAP.get(f.name, f.name))
        out[wire] = v
    return out


def _message(cls):
    """Decorator: attach to_dict / from_dict and register in TYPE_REGISTRY."""
    cls.to_dict = _to_dict
    cls.from_dict = classmethod(_from_dict)
    TYPE_REGISTRY[cls.TYPE] = cls
    return cls


TYPE_REGISTRY: dict[str, type] = {}


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


@_message
@dataclass
class ConnectClient:
    """Client → Server type ``c`` connect request."""

    TYPE: ClassVar[str] = "c"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"callsign": "c"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    name: str
    callsign: str
    last_message: int = 0
    last_emoji: int = 0
    last_edit: int = 0
    last_ham_ts: int = 0
    version: float = 0.1
    channels: list[dict] = field(default_factory=list)


@dataclass
class ConnectServer:
    """Server → Client type ``c`` connect ack."""

    TYPE: ClassVar[str] = "c_server"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    msg_count: int | None = None
    post_count: int | None = None
    welcome: int | None = None
    version: float | None = None


# Special: server's connect ack reuses the wire type "c" but has no callsign /
# name, so we disambiguate by inspecting fields. Register both decoders.
TYPE_REGISTRY["c"] = ConnectClient  # default; client-bound parser overrides


@_message
@dataclass
class Pairing:
    TYPE: ClassVar[str] = "p"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    from_call: str | None = None
    enabled: bool | None = None
    start_time: int | None = None


@_message
@dataclass
class UserEnquiry:
    TYPE: ClassVar[str] = "ue"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"callsign": "c"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    callsign: str
    registered: bool | None = None


@_message
@dataclass
class HamEnquiry:
    TYPE: ClassVar[str] = "he"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    hams: list = field(default_factory=list)


@_message
@dataclass
class UserConnect:
    TYPE: ClassVar[str] = "uc"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"callsign": "c"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    callsign: str = ""


@_message
@dataclass
class UserDisconnect:
    TYPE: ClassVar[str] = "ud"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"callsign": "c"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    callsign: str = ""


@_message
@dataclass
class OnlineUsers:
    TYPE: ClassVar[str] = "o"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"online": "o"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    online: list[str] = field(default_factory=list)


@_message
@dataclass
class UserUpdates:
    TYPE: ClassVar[str] = "u"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"users": "u"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    users: list = field(default_factory=list)


@_message
@dataclass
class KeepAlive:
    TYPE: ClassVar[str] = "k"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()


@_message
@dataclass
class Avatar:
    TYPE: ClassVar[str] = "a"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"avatar": "a"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    avatar: str | None = None
    callsign: str | None = None
    timestamp: int | None = None
    avatar_count: int | None = None


@_message
@dataclass
class AvatarResponse:
    TYPE: ClassVar[str] = "ar"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    timestamp: int | None = None


@_message
@dataclass
class AvatarEnquiry:
    TYPE: ClassVar[str] = "ae"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"last_avatar_ts": "lats"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    last_avatar_ts: int = 0
    count_only: int | None = None


@_message
@dataclass
class Stats:
    TYPE: ClassVar[str] = "s"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"stats": "s"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    stats: dict | None = None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@_message
@dataclass
class Message:
    TYPE: ClassVar[str] = "m"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"message": "m", "reply_id": "r"}
    _SERVER_ONLY: ClassVar[set[str]] = {"msg_status", "logged_ts"}

    from_call: str
    to_call: str
    message: str
    timestamp: int
    msg_id: str | None = None
    reply_id: str | None = None
    msg_status: int | None = None
    logged_ts: int | None = None


@_message
@dataclass
class MessageEdit:
    TYPE: ClassVar[str] = "med"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"message": "m"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    msg_id: str
    message: str
    edit_ts: int
    edited_flag: int | None = None


@_message
@dataclass
class MessageResponse:
    TYPE: ClassVar[str] = "mr"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    msg_id: str


@_message
@dataclass
class MessageEmoji:
    TYPE: ClassVar[str] = "mem"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"emoji": "e", "action": "a"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    msg_id: str
    emoji: Any  # str on send, list on receive
    emoji_ts: int
    action: int | None = None


@_message
@dataclass
class MessageBatch:
    TYPE: ClassVar[str] = "mb"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"meta": "md", "messages_field": "m"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    meta: dict
    messages_field: list


@_message
@dataclass
class MessageEditBatch:
    TYPE: ClassVar[str] = "medb"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"messages_field": "m"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    messages_field: list


@_message
@dataclass
class MessageEmojiBatch:
    TYPE: ClassVar[str] = "memb"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"messages_field": "m"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    messages_field: list


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


@_message
@dataclass
class ChannelPost:
    TYPE: ClassVar[str] = "cp"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"post": "p", "at_calls": "at"}
    _SERVER_ONLY: ClassVar[set[str]] = {"delivery_ts"}

    channel_id: int
    from_call: str
    timestamp: int
    post: str
    reply_ts: int | None = None
    reply_from: str | None = None
    gap: int | None = None
    # `at` on the wire — list of callsigns the post addresses (web-client
    # @-mention picker). The web client stores these out-of-band from the
    # body and renders them as styled tags before the body. Set on the
    # original `cp`; the matching `cped` wire frame doesn't carry it, so
    # mentions are immutable once a post is created.
    at_calls: list[str] | None = None
    delivery_ts: int | None = None


@_message
@dataclass
class ChannelPostEdit:
    TYPE: ClassVar[str] = "cped"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"post": "p"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    channel_id: int
    timestamp: int
    post: str
    edit_ts: int


@_message
@dataclass
class ChannelPostResponse:
    TYPE: ClassVar[str] = "cpr"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    timestamp: int
    delivery_ts: int | None = None


@_message
@dataclass
class ChannelPostEmoji:
    TYPE: ClassVar[str] = "cpem"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"emoji": "e", "action": "a"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    channel_id: int
    timestamp: int
    emoji_ts: int
    emoji: Any
    action: int | None = None


@_message
@dataclass
class ChannelSubscribe:
    TYPE: ClassVar[str] = "cs"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"subscribe": "s"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    channel_id: int
    subscribe: int
    last_channel_post: int | None = None
    post_count: int | None = None


@_message
@dataclass
class ChannelPostBatch:
    TYPE: ClassVar[str] = "cpb"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"meta": "m", "post_count": "pc", "posts": "p"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    channel_id: int
    meta: dict | None = None
    posts: list | None = None
    post_count: int | None = None  # client-only request form


@_message
@dataclass
class ChannelPostEditBatch:
    TYPE: ClassVar[str] = "cpedb"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"edits": "ed"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    edits: list = field(default_factory=list)


@_message
@dataclass
class ChannelPostEmojiBatch:
    TYPE: ClassVar[str] = "cpemb"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"emoji": "e"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    emoji: list = field(default_factory=list)


@_message
@dataclass
class PausedChannelHeaders:
    TYPE: ClassVar[str] = "pch"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"channel_headers": "ch"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    channel_headers: list = field(default_factory=list)


# Note: docs say "uc" for unpause-channel, but ``wps.py`` keys on "cu". Use cu.
@_message
@dataclass
class UnpauseChannel:
    """Client → Server unpause-channel request.

    Docs name this type ``uc`` but the server's dispatch table actually
    keys on ``cu`` (the docs reuse ``uc`` for User Connect, which is what
    the server really uses for that purpose). Mirror the code, not docs.
    """

    TYPE: ClassVar[str] = "cu"
    _WIRE_KEYS: ClassVar[dict[str, str]] = {"logged_ts": "lts"}
    _SERVER_ONLY: ClassVar[set[str]] = set()

    channel_id: int
    logged_ts: int | None = None
    post_count: int | None = None


def parse(d: dict) -> Any:
    """Decode a wire dict into the matching dataclass.

    Unknown types fall through as the raw dict so callers can still log them.
    """
    t = d.get("t")
    if t is None:
        return d
    cls = TYPE_REGISTRY.get(t)
    if cls is None:
        return d
    # Special: type "c" can be either ConnectClient (has 'n','c') or
    # ConnectServer (server reply with 'mc'/'pc'). The client only ever
    # *receives* the server form, so disambiguate.
    if t == "c" and "n" not in d:
        return ConnectServer(
            msg_count=d.get("mc"),
            post_count=d.get("pc"),
            welcome=d.get("w"),
            version=d.get("v"),
        )
    return cls.from_dict(d)
