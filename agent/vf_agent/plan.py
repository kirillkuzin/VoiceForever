"""Turn a "send" command from the server into keyboard actions. Pure logic, no Windows calls."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self


class Mode(StrEnum):
    ADDON = "addon"  # invisible edit box from the VoiceForever addon
    CHAT = "chat"  # normal chat box with slash commands


class Kind(StrEnum):
    KEY = "key"
    PASTE = "paste"
    SLEEP = "sleep"


class GameState(StrEnum):
    ACTIVE = "active"  # the game window is in front
    BACKGROUND = "background"  # the game runs, another window is in front
    CLOSED = "closed"  # no game window found


# Key names usable in combos like "ctrl+shift+f12", mapped to Windows virtual-key codes.
KEY_CODES: frozendict[str, int] = frozendict({
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    "pageup": 0x21, "pagedown": 0x22,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)},
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"num{d}": 0x60 + d for d in range(10)},
})
MODIFIERS = frozenset({"ctrl", "control", "shift", "alt"})


def parse_combo(combo: str) -> list[str]:
    """Split "Ctrl+Shift+F12" (or "CTRL-SHIFT-F12", as the addon writes it) into known key names."""
    keys = [k.strip().lower() for k in combo.replace("-", "+").split("+") if k.strip()]
    if not keys:
        raise ValueError("empty key combination")
    for key in keys:
        if key not in KEY_CODES:
            raise ValueError(f"unknown key {key!r} in {combo!r}")
    return keys


# Slash commands for chat mode (no addon): the agent opens the normal chat box.
SLASH: frozendict[str, str] = frozendict({
    "say": "/s",
    "yell": "/y",
    "emote": "/e",
    "group": "/p",  # without the addon we cannot tell party from raid; /p is the safe choice
    "party": "/p",
    "raid": "/raid",
    "raid_warning": "/rw",
    "instance": "/i",
    "guild": "/g",
    "officer": "/o",
    "whisper": "/w",
    "reply": "/r",
})

ADDON_PREFIX = "VF"


@dataclass(frozen=True, slots=True)
class Action:
    kind: Kind
    value: str | int  # key combo like "ctrl+shift+f12", text to paste, or milliseconds


@dataclass(frozen=True, slots=True)
class SendCommand:
    id: str
    channel: str
    target: str
    parts: tuple[str, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        match data:
            case {"id": str() | int() as msg_id, "parts": list(parts)} if all(
                isinstance(p, str) for p in parts
            ):
                cleaned = tuple(c for p in parts if (c := _clean(p)))
                return cls(
                    id=str(msg_id),
                    channel=str(data.get("channel", "say")),
                    target=str(data.get("target", "")),
                    parts=cleaned,
                )
            case _:
                raise ValueError("expected an id and parts as a list of strings")


def _clean(text: str) -> str:
    """Second line of defence after the server: one line, no escapes, no leading command."""
    text = " ".join(text.replace("|", "/").split())
    return text.lstrip("/. ")


def chat_line(channel: str, target: str, text: str) -> str:
    match channel:
        case "channel" if target.isdigit():
            return f"/{target} {text}"
        case "whisper" if target:
            return f"/w {target} {text}"
        case _:
            return f"{SLASH.get(channel, '/s')} {text}"


def addon_line(channel: str, target: str, text: str) -> str:
    return f"{ADDON_PREFIX}:{channel}:{target.replace(':', '')}:{text}"


def build_actions(
    cmd: SendCommand,
    mode: Mode,
    hotkey: str,
    open_delay_ms: int = 40,
    part_delay_ms: int = 300,
) -> list[Action]:
    actions: list[Action] = []
    for i, part in enumerate(cmd.parts):
        if i:
            actions.append(Action(Kind.SLEEP, part_delay_ms))
        match mode:
            case Mode.ADDON:
                opener, line = hotkey, addon_line(cmd.channel, cmd.target, part)
            case Mode.CHAT:
                opener, line = "enter", chat_line(cmd.channel, cmd.target, part)
        actions += [
            Action(Kind.KEY, opener),
            Action(Kind.SLEEP, open_delay_ms),
            Action(Kind.PASTE, line),
            Action(Kind.KEY, "enter"),
        ]
    return actions
