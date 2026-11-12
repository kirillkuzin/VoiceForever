"""Names and channels from the player's game, sent by the agent (agent/vf_agent/context.py).

The addon collects them (addon/VoiceForever/Context.lua): the joined chat channels and the
nicknames of the group, guild, friends and recent whisper partners and speakers. They help
speech recognition spell nicknames, let Claude pick whisper targets as the game spells them,
and let the player name a chat channel ("в канал Рейдеры"). Kept in memory only, never
written to disk or the log; the texts of other players' messages are never sent.
"""

import difflib
import re
import time
from dataclasses import dataclass
from typing import Any, Self

MAX_NAME = 64
LIMITS: frozendict[str, int] = frozendict({
    "channels": 20, "group": 40, "guild": 300, "friends": 100, "whispers": 20, "speakers": 30,
})
RECENT_S = 30 * 60  # whispers and speakers older than this are not "recent" any more
CHANNEL_KINDS = frozenset({"say", "yell", "party", "raid", "instance", "guild", "officer", "channel"})
_NAME_RE = re.compile(r"^\w[\w' .\-]*$")
# Russian case endings a spoken name may carry: "Арторну", "Арторна", "Арторном".
_ENDINGS = ("ами", "ями", "ом", "ем", "ой", "ей", "ою", "ею", "у", "ю", "а", "я", "е", "и", "ы")


def _name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = " ".join(value.split())
    return name if len(name) <= MAX_NAME and _NAME_RE.match(name) else ""


def _items(value: Any, key: str) -> list[Any]:
    return value[: LIMITS[key]] if isinstance(value, list) else []


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0
    return max(0, min(int(value), 10**9))


@dataclass(frozen=True, slots=True)
class GameChannel:
    n: int
    name: str


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    online: bool


@dataclass(frozen=True, slots=True)
class Recent:
    """Someone who whispered (dir "in"), was whispered ("out") or wrote in a channel (chan)."""

    name: str
    ago_s: int  # seconds before the snapshot was made
    dir: str = ""
    chan: str = ""
    channel: str = ""  # the custom channel's name for chan "channel"


@dataclass(frozen=True, slots=True)
class GameContext:
    made_at: float  # time.time() on the server when the game made the snapshot
    source: str  # "live" (copied from the game) or "file" (the addon's saved file)
    me: str = ""
    realm: str = ""
    channels: tuple[GameChannel, ...] = ()
    group: tuple[str, ...] = ()
    guild: tuple[Member, ...] = ()
    friends: tuple[Member, ...] = ()
    whispers: tuple[Recent, ...] = ()
    speakers: tuple[Recent, ...] = ()
    target: str = ""

    @classmethod
    def parse(cls, data: dict[str, Any], now: float | None = None) -> Self:
        """Check what the agent sent. Raises ValueError if it is not a snapshot of version 1."""
        now = time.time() if now is None else now
        if data.get("v") != 1:
            raise ValueError("unknown snapshot version")
        source = data.get("source")
        if source not in ("live", "file"):
            raise ValueError("unknown source")
        channels = []
        for ch in _items(data.get("channels"), "channels"):
            if isinstance(ch, dict) and isinstance(n := ch.get("n"), int) and 0 < n < 100:
                if name := _name(ch.get("name")):
                    channels.append(GameChannel(n, name))

        def members(key: str) -> tuple[Member, ...]:
            return tuple(
                Member(name, m.get("online") is True)
                for m in _items(data.get(key), key)
                if isinstance(m, dict) and (name := _name(m.get("name")))
            )

        def recent(key: str) -> tuple[Recent, ...]:
            out = []
            for e in _items(data.get(key), key):
                if not isinstance(e, dict) or not (name := _name(e.get("name"))):
                    continue
                direction = e.get("dir") if e.get("dir") in ("in", "out") else ""
                chan = e.get("chan") if e.get("chan") in CHANNEL_KINDS else ""
                out.append(Recent(name, _seconds(e.get("ago")), direction, chan, _name(e.get("channel"))))
            return tuple(out)

        return cls(
            made_at=now - _seconds(data.get("age_s")),
            source=source,
            me=_name(data.get("me")),
            realm=_name(data.get("realm")),
            channels=tuple(channels),
            group=tuple(n for g in _items(data.get("group"), "group") if (n := _name(g))),
            guild=members("guild"),
            friends=members("friends"),
            whispers=recent("whispers"),
            speakers=recent("speakers"),
            target=_name(data.get("target")),
        )

    # ------------------------------------------------------------------ reading
    def recent_whispers(self, now: float | None = None) -> list[tuple[Recent, int]]:
        """Whisper partners of the last half hour, newest first, with seconds ago from now."""
        return self._recent(self.whispers, now)

    def recent_speakers(self, now: float | None = None) -> list[tuple[Recent, int]]:
        return self._recent(self.speakers, now)

    def _recent(self, items: tuple[Recent, ...], now: float | None) -> list[tuple[Recent, int]]:
        now = time.time() if now is None else now
        out = []
        for r in items:
            ago = max(0, int(now - self.made_at) + r.ago_s)
            if ago <= RECENT_S:
                out.append((r, ago))
        return out

    def last_whisperer(self, now: float | None = None) -> str:
        """Who whispered the player last (in the last half hour), "" if nobody."""
        return next((r.name for r, _ in self.recent_whispers(now) if r.dir == "in"), "")

    def names(self, now: float | None = None) -> list[str]:
        """Every nickname, the likeliest to be said first: the target, recent whispers, the group,
        recent speakers, guild and friends online, then the rest."""
        ordered = [
            self.target,
            *(r.name for r, _ in self.recent_whispers(now)),
            *self.group,
            *(r.name for r, _ in self.recent_speakers(now)),
            *(m.name for m in self.guild if m.online),
            *(m.name for m in self.friends if m.online),
            *(m.name for m in self.guild if not m.online),
            *(m.name for m in self.friends if not m.online),
        ]
        return list(dict.fromkeys(n for n in ordered if n))

    def channel(self, spoken: str) -> GameChannel | None:
        """The joined channel with this number or name; a name may be said a little differently
        ("рейдеры" for "Рейдеры", "торговля" for "Торговля - Город")."""
        spoken = " ".join(spoken.split()).casefold()
        if not spoken:
            return None
        if spoken.isdigit():
            return next((c for c in self.channels if c.n == int(spoken)), None)
        by_name = {c.name.casefold(): c for c in self.channels}
        if spoken in by_name:
            return by_name[spoken]
        for c in self.channels:  # zone channels: "Торговля - Город", "General - Elwynn Forest"
            if c.name.casefold().split(" - ")[0] == spoken:
                return c
        for stem in _stems(spoken):
            for name, c in by_name.items():
                if name.split(" - ")[0].startswith(stem) and len(name.split(" - ")[0]) - len(stem) <= 3:
                    return c
        close = difflib.get_close_matches(spoken, list(by_name), n=1, cutoff=0.8)
        return by_name[close[0]] if close else None

    def match_name(self, spoken: str, now: float | None = None) -> str:
        """The game spelling of a nickname the player said, "" if it is nobody the game knows.

        Matches without the realm, in another case ("Арторну" -> "Арторн") and with small
        recognition errors; when several fit, the likeliest one (see names()) wins.
        """
        spoken = spoken.strip().casefold()
        if not spoken:
            return ""
        names = self.names(now)
        bases = {n: n.split("-")[0].casefold() for n in names}
        for n in names:
            if n.casefold() == spoken or bases[n] == spoken:
                return n
        for stem in _stems(spoken):
            for n in names:
                if bases[n] == stem or (bases[n].startswith(stem) and len(bases[n]) - len(stem) <= 1):
                    return n
        close = difflib.get_close_matches(spoken, list(bases.values()), n=1, cutoff=0.8)
        return next((n for n in names if close and bases[n] == close[0]), "")

    def counts(self) -> dict[str, int]:
        return {
            "channels": len(self.channels),
            "group": len(self.group),
            "guild": len(self.guild),
            "guild_online": sum(m.online for m in self.guild),
            "friends": len(self.friends),
        }


def _stems(word: str) -> list[str]:
    """The word without a Russian case ending, longest ending first."""
    return [word[: -len(e)] for e in _ENDINGS if word.endswith(e) and len(word) - len(e) >= 3]
