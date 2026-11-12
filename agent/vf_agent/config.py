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

from .i18n import LANGUAGES, t
from .plan import MODIFIERS, Mode, parse_combo
from .ptt import BY_NAME as PAD_BUTTONS
from .ptt import PttMode

PLACEHOLDER_URL = "wss://your-domain.example/ws"
PLACEHOLDER_TOKEN = "paste-agent-token-here"
MIN_TOKEN_LENGTH = 16

# (field, lowest, highest) for the numeric settings.
LIMITS: frozendict[str, tuple[float, float]] = frozendict({
    "focus_wait_s": (1, 120),
    "open_delay_ms": (0, 2000),
    "part_delay_ms": (0, 5000),
    "key_delay_ms": (0, 200),
    "max_record_s": (3, 120),
})
INT_FIELDS = frozenset({"open_delay_ms", "part_delay_ms", "key_delay_ms"})
BOOL_FIELDS = frozenset({
    "connect_on_start", "close_to_tray", "autostart", "ptt_enabled", "ptt_sounds", "game_context", "live_context",
})
LIST_FIELDS = frozenset({"window_titles", "process_names"})


class ConfigError(ValueError):
    """One or more settings are wrong. `errors` maps a setting name to a message for the window,
    in the window's language."""

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
    language: str = ""  # "" = the Windows display language, or one of i18n.LANGUAGES
    ptt_enabled: bool = True
    ptt_button: str = "rightstick"
    ptt_mode: PttMode = PttMode.TOGGLE
    mic_device: str = ""  # "" = the Windows default microphone
    max_record_s: float = 30
    ptt_sounds: bool = True
    game_context: bool = True
    live_context: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Check every setting and build the config; raises ConfigError listing all problems."""
        # With slots=True the class attributes are slot descriptors, so defaults come from fields().
        default = {f.name: f.default for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in default}
        errors: dict[str, str] = {}

        url = values.get("server_url", "")
        if not isinstance(url, str) or not url.strip() or url.strip() == PLACEHOLDER_URL:
            errors["server_url"] = t("cfg.server_url")
        else:
            url = values["server_url"] = url.strip()
            parts = urlsplit(url)
            if parts.scheme not in ("ws", "wss") or not parts.hostname:
                errors["server_url"] = t("cfg.server_url_scheme")

        token = values.get("token", "")
        if not isinstance(token, str) or PLACEHOLDER_TOKEN in token or not token.strip():
            errors["token"] = t("cfg.token")
        elif len(token.strip()) < MIN_TOKEN_LENGTH or any(c.isspace() for c in token.strip()):
            errors["token"] = t("cfg.token_short", n=MIN_TOKEN_LENGTH)
        else:
            values["token"] = token.strip()

        try:
            values["mode"] = Mode(values.get("mode", Mode.ADDON))
        except ValueError:
            errors["mode"] = t("cfg.mode")

        hotkey = values.get("addon_hotkey", default["addon_hotkey"])
        try:
            keys = parse_combo(hotkey) if isinstance(hotkey, str) else []
            if not keys or all(k in MODIFIERS for k in keys):
                raise ValueError
            values["addon_hotkey"] = "+".join(keys)
        except ValueError:
            errors["addon_hotkey"] = t("cfg.hotkey")

        for name in LIST_FIELDS:
            if name not in values:
                continue
            items = values[name]
            if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) for i in items):
                errors[name] = t("cfg.list")
            else:
                values[name] = tuple(dict.fromkeys(i.strip() for i in items if i.strip()))
        if not errors.keys() & LIST_FIELDS:
            titles = values.get("window_titles", default["window_titles"])
            processes = values.get("process_names", default["process_names"])
            if not titles and not processes:
                errors["process_names"] = t("cfg.window_or_process")

        for name, (low, high) in LIMITS.items():
            if name not in values:
                continue
            number = values[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                errors[name] = t("cfg.number")
            elif not low <= number <= high:
                errors[name] = t("cfg.range", low=f"{low:g}", high=f"{high:g}")
            elif name in INT_FIELDS:
                values[name] = round(number)

        for name in BOOL_FIELDS:
            if name in values and not isinstance(values[name], bool):
                errors[name] = t("cfg.bool")

        button = values.get("ptt_button", default["ptt_button"])
        if not isinstance(button, str) or button.strip().lower() not in PAD_BUTTONS:
            errors["ptt_button"] = t("cfg.ptt_button", buttons=", ".join(PAD_BUTTONS))
        else:
            values["ptt_button"] = button.strip().lower()
        try:
            values["ptt_mode"] = PttMode(values.get("ptt_mode", PttMode.TOGGLE))
        except ValueError:
            errors["ptt_mode"] = t("cfg.ptt_mode")
        mic = values.get("mic_device", "")
        if not isinstance(mic, str):
            errors["mic_device"] = t("cfg.mic")
        else:
            values["mic_device"] = mic.strip()
        language = values.get("language", "")
        if not isinstance(language, str) or language.strip().lower() not in {"", *LANGUAGES}:
            errors["language"] = t("cfg.language", codes=", ".join(LANGUAGES))
        else:
            values["language"] = language.strip().lower()

        if errors:
            raise ConfigError(errors)
        return cls(**values)

    @classmethod
    def load(cls, path: Path) -> Self:
        # utf-8-sig: Notepad and Windows PowerShell may put a BOM in front.
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError({"file": t("cfg.unreadable", error=e)}) from None
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-friendly values (lists, not tuples) for the window and for TOML."""
        return {k: list(v) if isinstance(v, tuple) else str(v) if isinstance(v, (Mode, PttMode)) else v
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
    "language": 'Window language: "" = as in Windows, or ' + ", ".join(LANGUAGES),
    "ptt_enabled": (
        "Push-to-talk: record your voice with a gamepad button and send it to the server\n"
        'ptt_button: rightstick, leftstick, misc1 (Xbox Share), back (View), a, b, x, y, dpup, ...\n'
        'ptt_mode: "toggle" = press to start, press again to send; "hold" = hold while talking'
    ),
    "mic_device": 'Microphone name as Windows shows it; "" = the default microphone',
    "max_record_s": "A recording is sent after this many seconds even if the button is not pressed again",
    "game_context": (
        "Names from the game (addon mode): your chat channels and the nicknames of your group, guild,\n"
        "friends and recent whispers go to the server, which keeps them in memory only\n"
        "live_context: copy fresh ones from the addon (Ctrl+C) when a recording starts"
    ),
})
BLANK_BEFORE = frozenset({"mode", "window_titles", "focus_wait_s", "connect_on_start", "ptt_enabled", "game_context"})


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
        return None, draft, {"file": t("cfg.unreadable", error=e)}
    try:
        config = AgentConfig.from_dict(raw)
    except ConfigError as e:
        draft |= {k: list(v) if isinstance(v, tuple) else v for k, v in raw.items() if k in draft}
        for key, placeholder in (("server_url", PLACEHOLDER_URL), ("token", PLACEHOLDER_TOKEN)):
            if draft[key] == placeholder:
                draft[key] = ""
        return None, draft, e.errors
    return config, config.to_dict(), {}


def peek_language(path: Path) -> str:
    """The language setting alone, read before the rest so problems are reported in it."""
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("language", "")
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return value.strip().lower() if isinstance(value, str) and value.strip().lower() in LANGUAGES else ""


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
