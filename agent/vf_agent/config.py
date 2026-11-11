"""Agent settings from config.toml (next to the .exe, or %APPDATA%\\VoiceForever).

The window edits the same file, so hand edits and the window never disagree.
"""

import json
import math
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from .plan import MODIFIERS, Mode, parse_combo

PLACEHOLDER_URL = "wss://your-domain.example/ws"
PLACEHOLDER_TOKEN = "paste-agent-token-here"
MIN_TOKEN_LENGTH = 16

# (field, lowest, highest) for the numeric settings.
LIMITS: frozendict[str, tuple[float, float]] = frozendict({
    "focus_wait_s": (1, 120),
    "open_delay_ms": (0, 2000),
    "part_delay_ms": (0, 5000),
    "key_delay_ms": (0, 200),
})
INT_FIELDS = frozenset({"open_delay_ms", "part_delay_ms", "key_delay_ms"})
BOOL_FIELDS = frozenset({"connect_on_start", "close_to_tray", "autostart"})
LIST_FIELDS = frozenset({"window_titles", "process_names"})


class ConfigError(ValueError):
    """One or more settings are wrong. `errors` maps a setting name to a message for the window."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{key}: {message}" for key, message in self.errors.items()))


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentConfig:
    server_url: str
    token: str
    mode: Mode = Mode.ADDON
    addon_hotkey: str = "ctrl+shift+f12"
    window_titles: tuple[str, ...] = ("World of Warcraft",)
    process_names: tuple[str, ...] = ("Wow.exe", "WowClassic.exe", "WowForever.exe", "Wow-64.exe")
    focus_wait_s: float = 15
    open_delay_ms: int = 40
    part_delay_ms: int = 350
    key_delay_ms: int = 15
    connect_on_start: bool = True
    close_to_tray: bool = True
    autostart: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Check every setting and build the config; raises ConfigError listing all problems."""
        # With slots=True the class attributes are slot descriptors, so defaults come from fields().
        default = {f.name: f.default for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in default}
        errors: dict[str, str] = {}

        url = values.get("server_url", "")
        if not isinstance(url, str) or not url.strip() or url.strip() == PLACEHOLDER_URL:
            errors["server_url"] = "Укажи адрес сервера, например wss://vf.example.com/ws"
        else:
            url = values["server_url"] = url.strip()
            parts = urlsplit(url)
            if parts.scheme not in ("ws", "wss") or not parts.hostname:
                errors["server_url"] = "Адрес должен начинаться с wss:// или ws://"

        token = values.get("token", "")
        if not isinstance(token, str) or PLACEHOLDER_TOKEN in token or not token.strip():
            errors["token"] = "Вставь токен агента из VF_USERS на сервере"
        elif len(token.strip()) < MIN_TOKEN_LENGTH or any(c.isspace() for c in token.strip()):
            errors["token"] = f"Токен из VF_USERS: не короче {MIN_TOKEN_LENGTH} символов, без пробелов"
        else:
            values["token"] = token.strip()

        try:
            values["mode"] = Mode(values.get("mode", Mode.ADDON))
        except ValueError:
            errors["mode"] = 'Режим: "addon" или "chat"'

        hotkey = values.get("addon_hotkey", default["addon_hotkey"])
        try:
            keys = parse_combo(hotkey) if isinstance(hotkey, str) else []
            if not keys or all(k in MODIFIERS for k in keys):
                raise ValueError
            values["addon_hotkey"] = "+".join(keys)
        except ValueError:
            errors["addon_hotkey"] = "Нужна клавиша, например ctrl+shift+f12"

        for name in LIST_FIELDS:
            if name not in values:
                continue
            items = values[name]
            if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) for i in items):
                errors[name] = "Нужен список строк"
            else:
                values[name] = tuple(dict.fromkeys(i.strip() for i in items if i.strip()))
        if not errors.keys() & LIST_FIELDS:
            titles = values.get("window_titles", default["window_titles"])
            processes = values.get("process_names", default["process_names"])
            if not titles and not processes:
                errors["process_names"] = "Нужно хотя бы одно название окна или процесс игры"

        for name, (low, high) in LIMITS.items():
            if name not in values:
                continue
            number = values[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                errors[name] = "Нужно число"
            elif not low <= number <= high:
                errors[name] = f"Допустимо от {low:g} до {high:g}"
            elif name in INT_FIELDS:
                values[name] = round(number)

        for name in BOOL_FIELDS:
            if name in values and not isinstance(values[name], bool):
                errors[name] = "Нужно true или false"

        if errors:
            raise ConfigError(errors)
        return cls(**values)

    @classmethod
    def load(cls, path: Path) -> Self:
        # utf-8-sig: Notepad and Windows PowerShell may put a BOM in front.
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError({"file": f"config.toml не читается: {e}"}) from None
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-friendly values (lists, not tuples) for the window and for TOML."""
        return {k: list(v) if isinstance(v, tuple) else str(v) if isinstance(v, Mode) else v
                for k, v in asdict(self).items()}

    def save(self, path: Path) -> None:
        write_atomic(path, render(self.to_dict()))


# Comment printed above each setting in config.toml.
COMMENTS: frozendict[str, str] = frozendict({
    "server_url": "Address of your server (wss:// behind TLS; ws:// only on a trusted network)",
    "token": 'Agent token: the part after ":" for your Telegram ID in VF_USERS on the server',
    "mode": (
        '"addon": uses the VoiceForever addon (chat window never opens, picks party/raid/instance)\n'
        '"chat":  no addon, types /p, /g, /w ... into the normal chat box'
    ),
    "addon_hotkey": "Must match the key set in the addon (/vf key CTRL-SHIFT-F12)",
    "window_titles": "How the agent recognises the game window",
    "focus_wait_s": "Wait this long for you to switch back to the game before giving up (seconds)",
    "open_delay_ms": "Pauses (milliseconds)",
    "connect_on_start": "Window and tray",
})
BLANK_BEFORE = frozenset({"mode", "window_titles", "focus_wait_s", "connect_on_start"})


def toml_value(value: Any) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case int() | float():
            return repr(value)
        case str():
            return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string
        case list() | tuple():
            return "[" + ", ".join(toml_value(v) for v in value) + "]"
        case _:
            raise TypeError(f"cannot write {value!r} to TOML")


def render(values: Mapping[str, Any]) -> str:
    lines = ["# VoiceForever Agent. The agent window edits this file; editing it by hand works too."]
    for f in fields(AgentConfig):
        if f.name not in values:
            continue
        if f.name in BLANK_BEFORE:
            lines.append("")
        if comment := COMMENTS.get(f.name):
            lines += [f"# {line}" for line in comment.splitlines()]
        lines.append(f"{f.name} = {toml_value(values[f.name])}")
    return "\n".join(lines) + "\n"


def defaults() -> dict[str, Any]:
    """Values for a new install: every default, with empty server and token."""
    return AgentConfig(server_url="", token="").to_dict()


TEMPLATE = render(defaults() | {"server_url": PLACEHOLDER_URL, "token": PLACEHOLDER_TOKEN})


def load_draft(path: Path) -> tuple[AgentConfig | None, dict[str, Any], dict[str, str]]:
    """For the window: the config if the file is valid, the values to show, and the problems."""
    draft = defaults()
    if not path.exists():
        return None, draft, {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        return None, draft, {"file": f"config.toml не читается: {e}"}
    try:
        config = AgentConfig.from_dict(raw)
    except ConfigError as e:
        draft |= {k: list(v) if isinstance(v, tuple) else v for k, v in raw.items() if k in draft}
        for key, placeholder in (("server_url", PLACEHOLDER_URL), ("token", PLACEHOLDER_TOKEN)):
            if draft[key] == placeholder:
                draft[key] = ""
        return None, draft, e.errors
    return config, config.to_dict(), {}


def default_path() -> Path:
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    local = Path(exe).resolve().parent / "config.toml"
    if local.exists():
        return local
    if appdata := os.environ.get("APPDATA"):
        return Path(appdata) / "VoiceForever" / "config.toml"
    return local


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_template(path: Path) -> None:
    write_atomic(path, TEMPLATE)
