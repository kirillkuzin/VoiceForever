"""Agent settings from config.toml (next to the .exe, or %APPDATA%\\VoiceForever)."""

import os
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Self

from .plan import Mode

TEMPLATE = """# VoiceForever Agent
# Address of your server (wss:// behind TLS; ws:// only on a trusted network)
server_url = "wss://your-domain.example/ws"
# Agent token: the part after ":" for your Telegram ID in VF_USERS on the server
token = "paste-agent-token-here"

# "addon": uses the VoiceForever addon (chat window never opens, picks party/raid/instance)
# "chat":  no addon, types /p, /g, /w ... into the normal chat box
mode = "addon"
# Must match the key set in the addon (/vf key CTRL-SHIFT-F12)
addon_hotkey = "ctrl+shift+f12"

# How the agent recognises the game window
window_titles = ["World of Warcraft"]
process_names = ["Wow.exe", "WowClassic.exe", "WowForever.exe", "Wow-64.exe"]

# Wait this long for you to switch back to the game before giving up (seconds)
focus_wait_s = 15
# Pauses (milliseconds)
open_delay_ms = 40
part_delay_ms = 350
key_delay_ms = 15
"""


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

    @classmethod
    def load(cls, path: Path) -> Self:
        data = tomllib.loads(path.read_text())
        known = {f.name for f in fields(cls)}
        values = {k: tuple(v) if isinstance(v, list) else v for k, v in data.items() if k in known}
        try:
            values["mode"] = Mode(values.get("mode", Mode.ADDON))
        except ValueError:
            raise ValueError('mode must be "addon" or "chat"') from None
        cfg = cls(**values)
        if not cfg.server_url.startswith(("ws://", "wss://")):
            raise ValueError("server_url must start with wss:// or ws://")
        if "paste-agent-token" in cfg.token or len(cfg.token) < 16:
            raise ValueError("set token in config.toml")
        return cfg


def default_path() -> Path:
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    local = Path(exe).resolve().parent / "config.toml"
    if local.exists():
        return local
    if appdata := os.environ.get("APPDATA"):
        return Path(appdata) / "VoiceForever" / "config.toml"
    return local


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE)
