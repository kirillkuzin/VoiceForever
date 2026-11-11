"""The agent window: a WebView2 page (pywebview), a tray icon and one running copy per user.

Threads: pywebview owns the main thread; the agent runs on the controller's loop thread;
the tray has its own; page events are pushed by one more, so a slow page never stalls the
connection.
"""

import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..client import ConnState, Status
from ..config import AgentConfig, load_draft
from ..controller import Controller
from . import winapi
from .api import Api, event_json
from .tray import MenuItem, Tray

log = logging.getLogger("vf_agent")

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
WIDTH, HEIGHT = 480, 720
DETECT_DELAY_S = 5
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

TRAY_DOT: frozendict[ConnState, str] = frozendict({
    ConnState.CONNECTED: "connected",
    ConnState.CONNECTING: "connecting",
    ConnState.RECONNECTING: "connecting",
    ConnState.DISCONNECTED: "disconnected",
    ConnState.AUTH_FAILED: "error",
    ConnState.REPLACED: "disconnected",
})
TRAY_TIP: frozendict[ConnState, str] = frozendict({
    ConnState.CONNECTED: "на связи",
    ConnState.CONNECTING: "подключение…",
    ConnState.RECONNECTING: "связь потеряна, переподключаюсь",
    ConnState.DISCONNECTED: "отключено",
    ConnState.AUTH_FAILED: "сервер не принял токен",
    ConnState.REPLACED: "агент подключён на другом ПК",
})


def make_backend(config: AgentConfig) -> Any:
    from ..winio import WindowsBackend

    return WindowsBackend(config.window_titles, config.process_names, config.key_delay_ms)


class App:
    def __init__(
        self, config_path: Path, log_path: Path, start_hidden: bool, instance: winapi.SingleInstance
    ) -> None:
        self.config_path = config_path
        self.log_path = log_path
        self.instance = instance
        self.config, self.draft, self.errors = load_draft(config_path)
        self.controller = Controller(make_backend, self._on_event)
        self.window: Any = None
        self.tray: Tray | None = None
        self.start_hidden = start_hidden and self.config is not None
        self.visible = not self.start_hidden
        self.quitting = False
        self._events: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        self._told_about_tray = False
        self._told_lost = False
        self._last_state = ConnState.DISCONNECTED

    # ------------------------------------------------------------ start and stop
    def run(self) -> int:
        import webview

        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True  # buttons in the title bar stay clickable
        self.window = webview.create_window(
            "VoiceForever",
            url=str(UI_DIR / "index.html"),
            js_api=Api(self),
            width=WIDTH,
            height=HEIGHT,
            resizable=False,
            frameless=True,
            easy_drag=False,
            background_color="#0b0907",
            hidden=self.start_hidden,
        )
        self.window.events.closing += self._on_closing
        self.window.events.loaded += lambda: log.info("window loaded")
        self.window.events.minimized += lambda: self._set_visible(False)
        self.window.events.restored += lambda: self._set_visible(True)
        try:
            webview.start(self._started, gui="edgechromium")
        finally:
            self._shutdown()
        return 0

    def _started(self) -> None:
        """Runs on its own thread once the window loop is up."""
        self.controller.start()
        self.tray = Tray(self.show, self._tray_menu)
        self.tray.start()
        threading.Thread(target=self._push_events, name="vf-ui-events", daemon=True).start()
        if self.config is not None:
            self._sync_autostart(self.config)
            self.controller.configure(self.config)
            if self.config.connect_on_start:
                self.controller.connect()
        self.instance.listen(self.show)

    def _shutdown(self) -> None:
        self._events.put(None)
        self.controller.shutdown()
        if self.tray is not None:
            self.tray.stop()
        self.instance.release()
        log.info("agent closed")

    # ------------------------------------------------------------ Host for the page
    def apply_config(self, config: AgentConfig) -> bool:
        self.config, self.draft, self.errors = config, config.to_dict(), {}
        self._sync_autostart(config)
        return self.controller.configure(config)

    def detect_window(self) -> dict[str, str]:
        from ..winio import foreground_window

        time.sleep(DETECT_DELAY_S)
        title, exe = foreground_window()
        if exe.lower() == Path(sys.executable).name.lower():
            return {"title": "", "exe": ""}  # the agent itself is still in front
        return {"title": title, "exe": exe}

    def open_path(self, path: Path) -> None:
        if path.exists():
            os.startfile(path)  # Explorer or the default editor

    def minimize(self) -> None:
        self.window.minimize()

    def close(self) -> None:
        """The cross in the title bar: to the tray, or quit if that is turned off."""
        if self.config is not None and self.config.close_to_tray:
            self.hide()
        else:
            self.quit()

    def quit(self) -> None:
        self.quitting = True
        self.window.destroy()

    # ------------------------------------------------------------ window
    def show(self) -> None:
        self.window.show()
        self.window.restore()
        self._set_visible(True)

    def hide(self) -> None:
        self.window.hide()
        self._set_visible(False)
        if not self._told_about_tray and self.tray is not None:
            self._told_about_tray = True
            self.tray.notify(
                "VoiceForever работает в трее",
                "Агент остаётся на связи. Окно открывается щелчком по значку.",
            )

    def _set_visible(self, visible: bool) -> None:
        self.visible = visible

    def _on_closing(self) -> bool:
        """Alt+F4 and the taskbar's Close. Returning False keeps the agent running in the tray."""
        if self.quitting or self.config is None or not self.config.close_to_tray:
            self.quitting = True
            return True
        threading.Thread(target=self.hide, daemon=True).start()
        return False

    # ------------------------------------------------------------ events
    def _on_event(self, seq: int, event: Any) -> None:
        self._events.put(event_json(seq, event))
        if isinstance(event, Status):
            self._on_status(event)

    def _push_events(self) -> None:
        while (event := self._events.get()) is not None:
            script = f"window.vf && window.vf.onEvent({json.dumps(event, ensure_ascii=False)})"
            try:
                self.window.run_js(script)
            except Exception as e:  # the page is reloading or closing; it re-reads the state when ready
                log.debug("could not pass an event to the page: %s", e)

    def _on_status(self, status: Status) -> None:
        state = status.state
        if self.tray is not None:
            self.tray.set_state(TRAY_DOT[state], f"VoiceForever: {TRAY_TIP[state]}")
            if not self.visible:
                self._notify(state, status.detail)
        if state is ConnState.CONNECTED:
            self._told_lost = False
        self._last_state = state

    def _notify(self, state: ConnState, detail: str) -> None:
        assert self.tray is not None
        match state:
            case ConnState.RECONNECTING if self._last_state is ConnState.CONNECTED:
                self._told_lost = True
                self.tray.notify("Связь с сервером потеряна", f"Переподключаюсь сам. Причина: {detail}.", warning=True)
            case ConnState.CONNECTED if self._told_lost:
                self.tray.notify("Связь восстановлена", "Сообщения снова уходят в игру.")
            case ConnState.AUTH_FAILED:
                self.tray.notify("Сервер не принял токен", "Открой окно агента и проверь токен в настройках.", warning=True)
            case ConnState.REPLACED:
                self.tray.notify(
                    "Агент подключён на другом ПК",
                    "Этот агент отключился. Подключить его снова можно из окна или меню значка.",
                    warning=True,
                )

    # ------------------------------------------------------------ tray
    def _tray_menu(self) -> list[MenuItem]:
        items: list[MenuItem] = [("Открыть VoiceForever", self.show), None]
        if self.config is not None:
            if self.controller.running:
                items += [("Отключить", self.controller.disconnect), ("Переподключить", self.controller.reconnect)]
            else:
                items.append(("Подключить", self.controller.connect))
            items += [("Тест в /say", self.controller.send_test), None]
        items.append(("Выход", self.quit))
        return items

    def _sync_autostart(self, config: AgentConfig) -> None:
        try:
            winapi.set_autostart(config.autostart, self.config_path)
        except OSError:
            log.exception("could not change autostart")


def run(config_path: Path, log_path: Path, start_hidden: bool = False) -> int:
    instance = winapi.SingleInstance()
    if not instance.acquire():
        log.info("the agent is already running: showing its window")
        instance.show_running()
        return 0
    try:
        from webview.platforms import winforms  # loads .NET and looks for WebView2
    except Exception:
        log.exception("the window could not start")
        winapi.message_box(
            "Окно агента не запустилось: не загрузился .NET Framework.\n\n"
            f"Подробности в журнале:\n{log_path}\n\n"
            "Без окна агент работает так: VoiceForeverAgent.exe --headless"
        )
        return 1
    if not getattr(winforms, "is_chromium", True):
        winapi.message_box(
            "Для окна агента нужен Microsoft Edge WebView2 Runtime. "
            f"В Windows 11 он уже есть, для Windows 10 его можно скачать здесь:\n{WEBVIEW2_URL}\n\n"
            "Без окна агент работает так: VoiceForeverAgent.exe --headless"
        )
        return 1
    return App(config_path, log_path, start_hidden, instance).run()
