"""Names and channels from the game, for the server: nicknames spelled as in the game (speech
recognition hint, whisper targets) and chat channels by name.

The addon keeps a snapshot (addon/VoiceForever/Context.lua). It reaches the agent two ways:
from the addon's saved file, which the game writes on logout and /reload, and live: the agent
asks the addon for it and copies it out of the addon's edit box (plan.export_actions). Only
names, channel numbers and how long ago someone wrote are in it, never what they wrote.
"""

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .plan import EXPORT_PREFIX

VERSION = 1
# The most of each list the agent passes on; the addon keeps the same or fewer.
LIMITS: frozendict[str, int] = frozendict({
    "channels": 20,
    "group": 40,
    "guild": 300,
    "friends": 100,
    "whispers": 20,
    "speakers": 30,
})
MAX_NAME = 64  # characters; "Name-Realm" fits easily
MAX_EXPORT_CHARS = 100_000
CHANNEL_KINDS = frozenset({
    "say", "yell", "party", "raid", "instance", "guild", "officer", "channel",
})


class Source(StrEnum):
    LIVE = "live"  # copied from the game just now
    FILE = "file"  # the addon's saved file, as of the last logout or /reload


@dataclass(frozen=True, slots=True)
class GameContext:
    data: dict[str, Any]  # the checked snapshot, ready for JSON
    source: Source
    at: float  # time.time() when the game made it (the game and the agent share the PC's clock)

    def counts(self) -> dict[str, int]:
        d = self.data
        return {
            "channels": len(d["channels"]),
            "group": len(d["group"]),
            "guild": len(d["guild"]),
            "guild_online": sum(1 for m in d["guild"] if m["online"]),
            "friends": len(d["friends"]),
            "recent": len({e["name"] for e in [*d["whispers"], *d["speakers"]]}),
        }

    def message(self, now: float | None = None) -> dict[str, Any]:
        """What the server gets. Ages count from now, so the server's clock does not matter."""
        now = time.time() if now is None else now
        return {"type": "context", "source": str(self.source), "age_s": max(0, round(now - self.at))} | self.data


def _name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = " ".join(value.replace("|", "").split())
    return name if 0 < len(name) <= MAX_NAME else ""


def _items(value: Any, limit: int) -> list[Any]:
    """A Lua list from the saved file comes as a list, or as a dict when it was empty."""
    return list(value)[:limit] if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def normalize(raw: Any) -> dict[str, Any] | None:
    """The snapshot with only the expected fields, each checked and capped; None if it is not one."""
    if not isinstance(raw, dict) or raw.get("v") != VERSION or _number(raw.get("at")) is None:
        return None
    channels = []
    for ch in _items(raw.get("channels"), LIMITS["channels"]):
        if isinstance(ch, dict) and (n := _number(ch.get("n"))) is not None and 0 < n < 100:
            if name := _name(ch.get("name")):
                channels.append({"n": int(n), "name": name})
    group = [n for item in _items(raw.get("group"), LIMITS["group"]) if (n := _name(item))]

    def people(key: str) -> list[dict[str, Any]]:
        out = []
        for item in _items(raw.get(key), LIMITS[key]):
            if isinstance(item, dict) and (name := _name(item.get("name"))):
                out.append({"name": name, "online": item.get("online") is True})
        return out

    def aged(key: str, extra: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in _items(raw.get(key), LIMITS[key]):
            if not isinstance(item, dict) or not (name := _name(item.get("name"))):
                continue
            entry: dict[str, Any] = {"name": name, "ago": max(0, round(_number(item.get("ago")) or 0))}
            for field, check in extra.items():
                if check(value := item.get(field)):
                    entry[field] = value if field != "channel" else _name(value)
            out.append(entry)
        return out

    return {
        "v": VERSION,
        "me": _name(raw.get("me")),
        "realm": _name(raw.get("realm")),
        "locale": _name(raw.get("locale"))[:8],
        "channels": channels,
        "group": group,
        "guild": people("guild"),
        "friends": people("friends"),
        "whispers": aged("whispers", {"dir": lambda v: v in ("in", "out")}),
        "speakers": aged("speakers", {
            "chan": lambda v: v in CHANNEL_KINDS,
            "channel": lambda v: bool(_name(v)),
        }),
        "target": _name(raw.get("target")),
    }


def parse_export(text: str | None, nonce: str, received: float | None = None) -> GameContext | None:
    """The snapshot copied from the addon's edit box: "VFX1 <nonce> <json>"."""
    head = f"{EXPORT_PREFIX} {nonce} "
    if not text or not text.startswith(head) or len(text) > MAX_EXPORT_CHARS:
        return None
    try:
        raw = json.loads(text[len(head):])
    except ValueError:
        return None
    if (data := normalize(raw)) is None:
        return None
    # The snapshot was made a moment ago; its own `at` has whole seconds only.
    return GameContext(data, Source.LIVE, time.time() if received is None else received)


def from_saved(db: dict[str, Any]) -> GameContext | None:
    """The newest character snapshot in the addon's saved file (VoiceForeverDB.chars)."""
    chars = db.get("chars")
    if not isinstance(chars, dict):
        return None
    best: GameContext | None = None
    for raw in chars.values():
        if (data := normalize(raw)) is not None and (best is None or raw["at"] > best.at):
            best = GameContext(data, Source.FILE, float(raw["at"]))
    return best
