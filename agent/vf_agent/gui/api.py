"""What the page may call, as window.pywebview.api.<name>(...). Every method returns plain JSON data.

pywebview runs each call on its own thread, so slow calls (the typing test, finding the game
window) do not freeze the window.
"""

import dataclasses
import logging
from pathlib import Path
from typing import Any, Protocol

from .. import __version__, i18n
from ..client import BindCheck, Delivery, Event, Game, MicLevel, PadState, Recording, Status, VoiceResult
from ..config import AgentConfig, ConfigError, defaults
from ..controller import Controller
from ..ptt import PAD_BUTTONS, button_label

log = logging.getLogger("vf_agent")

# Settings the first-run wizard asks for. Problems elsewhere in config.toml go to the settings tab.
WIZARD_FIELDS = frozenset({"server_url", "token"})


def status_json(status: Status) -> dict[str, Any]:
    return {
        "state": str(status.state),
        "detail": status.detail,
        "attempt": status.attempt,
        "retry_at": status.retry_at,
        "since": status.since,
    }


def delivery_json(delivery: Delivery | VoiceResult) -> dict[str, Any]:
    """A row of the message list: a typed message, or what became of a recording."""
    kind = "voice" if isinstance(delivery, VoiceResult) else "delivery"
    return {"kind": kind} | dataclasses.asdict(delivery)


def recording_json(recording: Recording) -> dict[str, Any]:
    return {"state": str(recording.state), "started_at": recording.started_at, "max_s": recording.max_s}


def pad_buttons_json() -> list[dict[str, str]]:
    """Every push-to-talk button with its names on Xbox and DualSense, in the current language."""
    return [{"name": b.name, "xbox": b.xbox, "playstation": b.playstation} for b in PAD_BUTTONS]


def language_json(setting: str) -> dict[str, Any]:
    """The language part of the page's state: the setting, the language in use and its strings."""
    return {
        "language": setting,
        "lang": i18n.current(),
        "system_lang": i18n.system_language(),
        "strings": dict(i18n.strings(i18n.current())),
    }


def event_json(seq: int, event: Event) -> dict[str, Any]:
    match event:
        case Status():
            return {"type": "status", "seq": seq, "status": status_json(event)}
        case Delivery():
            return {"type": "delivery", "seq": seq, "delivery": delivery_json(event)}
        case VoiceResult():
            return {"type": "voice", "seq": seq, "voice": delivery_json(event)}
        case Game(state):
            return {"type": "game", "seq": seq, "game": str(state)}
        case PadState():
            return {"type": "pad", "seq": seq, "pad": dataclasses.asdict(event)}
        case Recording():
            return {"type": "recording", "seq": seq, "recording": recording_json(event)}
        case MicLevel(level):
            return {"type": "mic", "seq": seq, "level": level}
        case BindCheck():
            return {"type": "bind_check", "seq": seq, "check": dataclasses.asdict(event)}


class Host(Protocol):
    """The app around the window (gui.app.App); tests use a fake one."""

    controller: Controller
    config: AgentConfig | None
    config_path: Path
    log_path: Path
    draft: dict[str, Any]
    errors: dict[str, str]

    def apply_config(self, config: AgentConfig) -> bool: ...
    def set_language(self, setting: str) -> None: ...
    def file_errors(self) -> dict[str, str]: ...
    def detect_window(self) -> dict[str, str]: ...
    def open_path(self, path: Path) -> None: ...
    def minimize(self) -> None: ...
    def close(self) -> None: ...
    def quit(self) -> None: ...


class Api:
    def __init__(self, host: Host) -> None:
        self._host = host

    def get_state(self) -> dict[str, Any]:
        host = self._host
        snapshot = host.controller.snapshot()
        log.info("window is ready")
        config = host.config.to_dict() if host.config is not None else host.draft
        return language_json(str(config.get("language", ""))) | {
            "version": __version__,
            "languages": [{"code": code, "name": name} for code, name in i18n.LANGUAGES.items()],
            "configured": host.config is not None,
            "first_run": host.config is None and host.errors.keys() <= WIZARD_FIELDS,
            "config": config,
            "errors": host.errors,
            "defaults": defaults(),
            "config_path": str(host.config_path),
            "seq": snapshot["seq"],
            "status": status_json(snapshot["status"]),
            "game": str(snapshot["game"]),
            "history": [delivery_json(d) for d in snapshot["history"]],
            "pad": dataclasses.asdict(snapshot["pad"]),
            "recording": recording_json(snapshot["recording"]),
            "bind_check": dataclasses.asdict(c) if (c := snapshot["bind_check"]) else None,
            "pad_buttons": pad_buttons_json(),
        }

    def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        """Check and save the settings, then use them. Errors come back per field."""
        host = self._host
        try:
            config = AgentConfig.from_dict(values)
        except ConfigError as e:
            return {"ok": False, "errors": e.errors}
        try:
            config.save(host.config_path)
        except OSError as e:
            return {"ok": False, "errors": {"file": i18n.t("cfg.not_written", path=host.config_path, error=e.strerror or e)}}
        log.info("settings saved to %s", host.config_path)
        reconnecting = host.apply_config(config)
        return {"ok": True, "errors": {}, "config": config.to_dict(), "reconnecting": reconnecting}

    def check_config(self, values: dict[str, Any]) -> dict[str, str]:
        """The problems with these settings, without saving them: after a language change the page
        shows the same problems again in the new language."""
        try:
            AgentConfig.from_dict(values)
        except ConfigError as e:
            return e.errors | self._host.file_errors()
        return self._host.file_errors()

    def set_language(self, setting: str) -> dict[str, Any]:
        """Switch the window, the tray and new messages to a language ("" = as in Windows) and keep
        the choice in config.toml. Returns what the page needs to draw itself again."""
        setting = str(setting or "").strip().lower()
        if setting and setting not in i18n.LANGUAGES:
            setting = ""
        self._host.set_language(setting)
        return language_json(setting) | {"pad_buttons": pad_buttons_json()}

    def connect(self) -> None:
        if self._host.config is not None:
            self._host.controller.connect()

    def disconnect(self) -> None:
        self._host.controller.disconnect()

    def reconnect(self) -> None:
        if self._host.config is not None:
            self._host.controller.reconnect()

    def send_test(self) -> dict[str, str]:
        config = self._host.config
        if config is None:
            return {"status": "error", "detail": i18n.t("bind.d_save_first")}
        result = self._host.controller.send_test().result(timeout=config.focus_wait_s + 30)
        return {"status": result["status"], "detail": result["detail"]}

    # ------------------------------------------------------------ push-to-talk
    def list_mics(self) -> dict[str, Any]:
        """Microphones Windows lists, by name. "" in mic_device means the default one."""
        try:
            return {"mics": self._host.controller.microphones(), "error": ""}
        except Exception as e:
            return {"mics": [], "error": str(e)}

    def mic_test(self, on: bool, device: str = "") -> dict[str, str]:
        """Open the microphone to show its level as "mic" events; on=False closes it."""
        try:
            self._host.controller.mic_test(bool(on), str(device or ""))
        except Exception as e:
            return {"error": str(e)}
        return {"error": ""}

    def capture_pad_button(self, timeout_s: float = 20) -> dict[str, Any]:
        """Wait for the next pad press. The page then saves its name as ptt_button."""
        press = self._host.controller.capture_button(float(timeout_s))
        if press is None:
            return {"button": "", "error": "timeout"}  # or cancelled from the page
        return {
            "button": press.button,
            "label": button_label(press.button, press.pad.playstation),
            "pad_name": press.pad.name,
            "error": "",
        }

    def cancel_pad_capture(self) -> None:
        self._host.controller.cancel_capture()

    def check_game_binding(self) -> None:
        """Start the in-game check; steps arrive as "bind_check" events."""
        self._host.controller.check_binding()

    def cancel_game_binding(self) -> None:
        self._host.controller.cancel_binding()

    def detect_game_window(self) -> dict[str, str]:
        """Wait a few seconds, then report the window in front: the player switches to the game."""
        return self._host.detect_window()

    def open_config_folder(self) -> None:
        self._host.open_path(self._host.config_path.parent)

    def open_log(self) -> None:
        self._host.open_path(self._host.log_path)

    def minimize(self) -> None:
        self._host.minimize()

    def close(self) -> None:
        self._host.close()

    def quit(self) -> None:
        self._host.quit()
