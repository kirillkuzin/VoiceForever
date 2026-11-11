"""What the page may call, as window.pywebview.api.<name>(...). Every method returns plain JSON data.

pywebview runs each call on its own thread, so slow calls (the typing test, finding the game
window) do not freeze the window.
"""

import dataclasses
import logging
from pathlib import Path
from typing import Any, Protocol

from .. import __version__
from ..client import Delivery, Event, Game, Status
from ..config import AgentConfig, ConfigError, defaults
from ..controller import Controller

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


def delivery_json(delivery: Delivery) -> dict[str, Any]:
    return dataclasses.asdict(delivery)


def event_json(seq: int, event: Event) -> dict[str, Any]:
    match event:
        case Status():
            return {"type": "status", "seq": seq, "status": status_json(event)}
        case Delivery():
            return {"type": "delivery", "seq": seq, "delivery": delivery_json(event)}
        case Game(state):
            return {"type": "game", "seq": seq, "game": str(state)}


class Host(Protocol):
    """The app around the window (gui.app.App); tests use a fake one."""

    controller: Controller
    config: AgentConfig | None
    config_path: Path
    log_path: Path
    draft: dict[str, Any]
    errors: dict[str, str]

    def apply_config(self, config: AgentConfig) -> bool: ...
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
        return {
            "version": __version__,
            "configured": host.config is not None,
            "first_run": host.config is None and host.errors.keys() <= WIZARD_FIELDS,
            "config": host.config.to_dict() if host.config is not None else host.draft,
            "errors": host.errors,
            "defaults": defaults(),
            "config_path": str(host.config_path),
            "seq": snapshot["seq"],
            "status": status_json(snapshot["status"]),
            "game": str(snapshot["game"]),
            "history": [delivery_json(d) for d in snapshot["history"]],
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
            return {"ok": False, "errors": {"file": f"Не удалось записать {host.config_path}: {e.strerror or e}"}}
        log.info("settings saved to %s", host.config_path)
        reconnecting = host.apply_config(config)
        return {"ok": True, "errors": {}, "config": config.to_dict(), "reconnecting": reconnecting}

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
            return {"status": "error", "detail": "сначала сохрани настройки"}
        result = self._host.controller.send_test().result(timeout=config.focus_wait_s + 30)
        return {"status": result["status"], "detail": result["detail"]}

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
