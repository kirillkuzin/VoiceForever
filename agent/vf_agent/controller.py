"""Runs the agent on its own asyncio loop thread, so the window and the tray can drive it.

Every public method is safe to call from any thread. Events reach `on_event` on the loop
thread, numbered by `seq`, so a window that asked for a snapshot can drop older events.
"""

import asyncio
import contextlib
import itertools
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from . import savedvars
from .client import (
    Ack,
    Agent,
    AuthError,
    BindCheck,
    ConnState,
    Delivery,
    Event,
    Game,
    InputBackend,
    MicLevel,
    Names,
    PadState,
    RecState,
    Recording,
    ReplacedError,
    Status,
    VoiceResult,
)
from .config import AgentConfig
from .context import GameContext, from_saved
from .devices import Devices, Pad, Press
from .i18n import t
from .plan import GameState, Mode, SendCommand
from .ptt import BY_NAME, Cmd, PttMachine, button_label

log = logging.getLogger("vf_agent")

type BackendFactory = Callable[[AgentConfig], InputBackend]
type SeqListener = Callable[[int, Event], None]
type DevicesFactory = Callable[[Callable[[Press], None], Callable[[Pad | None], None], Callable[[float], None]], Devices]

GAME_POLL_S = 1.0
HISTORY = 50
RECORD_TICK_S = 0.1
BIND_WAIT_GAME_S = 600  # the player starts the game and logs in
BIND_WAIT_ADDON_S = 120  # the player presses the button in the game, the UI reloads
ADDON_NOT_SET_UP = ("", -1.0)  # _addon_synced when the addon has no push-to-talk button yet
ADDON_REREAD_S = 10.0  # until then, how often to look whether it got one (/vf ptt in the game)
SAVED_NAMES_EVERY_S = 5.0  # how often to look whether the game wrote the addon's file (/reload, logout)
LIVE_NAMES_EVERY_S = 20.0  # the least time between two copies of fresh names from the game
LIVE_NAMES_BACKOFF_S = (60.0, 600.0)  # pauses after copies that failed: first, longest
LIVE_NAMES_WAIT_S = 2.0  # how long a recording waits for the names copied at its start
# Recording statuses with a line of their own on the addon's badge in the game.
BADGE_STATUSES = frozenset({
    "skip", "empty", "stt_error", "bad_audio", "too_long", "unsupported", "offline", "timeout", "error",
})


def badge_text(status: str) -> str:
    """What the addon's badge in the game says when a recording did not become a message."""
    return t(f"badge.{status}" if status in BADGE_STATUSES else "badge.error")


class Controller:
    def __init__(
        self,
        make_backend: BackendFactory,
        on_event: SeqListener | None = None,
        make_devices: DevicesFactory | None = None,
    ) -> None:
        self.make_backend = make_backend
        self.on_event = on_event
        self.make_devices = make_devices
        self.config: AgentConfig | None = None
        self.status = Status(ConnState.DISCONNECTED)
        self.game = GameState.CLOSED
        self.history: deque[Delivery | VoiceResult] = deque(maxlen=HISTORY)
        self.seq = 0
        self.pad = PadState(False)
        self.recording = Recording(RecState.IDLE)
        self.bind_check: BindCheck | None = None
        self.names = Names()
        self.context: GameContext | None = None  # names and channels from the game, for the server
        self.devices: Devices | None = None
        self._agent: Agent | None = None
        self._session: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None
        self._ptt = PttMachine()
        self._ticker: asyncio.Task[None] | None = None
        self._capture: asyncio.Future[Press | None] | None = None
        self._bind_task: asyncio.Task[None] | None = None
        self._mic_test = False
        self._sending = 0  # recordings sent and not answered yet
        self._addon_synced: tuple[str, float] | None = None  # (mode, max_s) the addon has
        self._addon_read_at = 0.0  # time.monotonic() of the last look at its saved file
        self._exe: Path | None = None  # the game's exe, kept after it closes: logout writes the file
        self._saved_checked_at = -SAVED_NAMES_EVERY_S  # time.monotonic() of the last look for names
        self._saved_mtime: float | None = None  # of the saved file the names last came from
        self._live_at = -LIVE_NAMES_EVERY_S  # time.monotonic() of the last copy from the game
        self._live_pause = 0.0  # grows while copies fail (an addon without the export)
        self._export: asyncio.Task[None] | None = None
        self._test_ids = itertools.count(1)
        self._lock = threading.Lock()  # guards the snapshot fields above against the window's reads
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="vf-agent", daemon=True)

    # ------------------------------------------------------------ thread-safe API
    def start(self) -> None:
        self._thread.start()
        self._call(self._start_poller)
        if self.make_devices is not None:
            self._call(self._start_devices)

    def configure(self, config: AgentConfig) -> bool:
        """Use new settings. Returns True when this reconnects (server, token or mode changed)."""
        return self._call(self._configure, config).result()

    def connect(self) -> None:
        self._call(self._connect)

    def disconnect(self) -> None:
        self._call(self._disconnect).result()

    def reconnect(self) -> None:
        self._call(self._reconnect)

    def send_test(self) -> Future[Ack]:
        """Type a test line into /say, the same way a real message is typed."""
        cmd = SendCommand(f"test-{next(self._test_ids)}", "say", "", ("VoiceForever test",))
        return self._call(self._handle, cmd)

    def microphones(self) -> list[str]:
        if self.devices is None:
            return []
        return self.devices.microphones().result(timeout=10)

    def mic_test(self, on: bool, device: str = "") -> None:
        """Open the microphone only to show its level (the wizard and the settings)."""
        self._call(self._set_mic_test, on, device).result(timeout=10)

    def capture_button(self, timeout: float = 20.0) -> Press | None:
        """Wait for the next press on any pad button: the window assigns the push-to-talk button."""
        return self._call(self._capture_press, timeout).result(timeout=timeout + 5)

    def cancel_capture(self) -> None:
        """Stop waiting for a press (Esc in the window); capture_button then returns None."""
        self._call(self._cancel_capture)

    def check_binding(self) -> None:
        """Start the in-game check of the push-to-talk button; steps arrive as BindCheck events."""
        self._call(self._start_bind_check)

    def cancel_binding(self) -> None:
        self._call(self._cancel_bind_check)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seq": self.seq,
                "status": self.status,
                "game": self.game,
                "history": list(self.history),
                "pad": self.pad,
                "recording": self.recording,
                "bind_check": self.bind_check,
                "names": self.names,
            }

    def shutdown(self, timeout: float = 5.0) -> None:
        if not self._thread.is_alive():
            return
        with contextlib.suppress(Exception):
            self._call(self._stop_all).result(timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
        if self.devices is not None:
            self.devices.stop()

    # ------------------------------------------------------------ loop thread
    def _call[T](self, fn: Callable[..., Any], *args: Any) -> Future[T]:
        async def run() -> T:
            return await fn(*args)

        return asyncio.run_coroutine_threadsafe(run(), self._loop)

    def _emit(self, event: Event) -> None:
        with self._lock:
            self.seq += 1
            seq = self.seq
            match event:
                case Status():
                    self.status = event
                case Delivery() | VoiceResult():
                    self.history.appendleft(event)
                case Game(state):
                    self.game = state
                case PadState():
                    self.pad = event
                case Recording():
                    self.recording = event
                case BindCheck():
                    self.bind_check = event
                case Names():
                    self.names = event
        if self.on_event is not None:
            try:
                self.on_event(seq, event)
            except Exception:
                log.exception("event listener failed")

    @property
    def running(self) -> bool:
        return self._session is not None and not self._session.done()

    async def _configure(self, config: AgentConfig) -> bool:
        old, self.config = self.config, config
        if (self._ptt.mode, self._ptt.max_s) != (config.ptt_mode, config.max_record_s) or not config.ptt_enabled:
            await self._finish(self._ptt.abort())
        self._ptt.mode, self._ptt.max_s = config.ptt_mode, config.max_record_s
        backend = self.make_backend(config)
        await self._configure_names(old)
        if self._agent is None:
            self._agent = self._new_agent(config, backend)
            return False
        if (old.server_url, old.token, old.mode) == (config.server_url, config.token, config.mode):
            # Input settings apply to the next message; the connection stays as it is.
            self._agent.config, self._agent.backend = config, backend
            return False
        # A new token is the usual fix for a rejected one, so try again right away.
        retry = self.running or self.status.state in (ConnState.AUTH_FAILED, ConnState.REPLACED)
        await self._disconnect()
        self._agent = self._new_agent(config, backend)
        if retry:
            await self._connect()
        return retry

    def _new_agent(self, config: AgentConfig, backend: InputBackend) -> Agent:
        agent = Agent(config, backend, self._emit)
        agent.context = self.context  # the server gets it after connecting
        return agent

    async def _connect(self) -> None:
        if self._agent is None or self.running:
            return
        self._session = asyncio.create_task(self._run(self._agent))

    async def _run(self, agent: Agent) -> None:
        try:
            await agent.run_forever()
        except (AuthError, ReplacedError) as e:
            log.warning("agent stopped: %s", e)
        except Exception:
            log.exception("agent crashed")
            self._emit(Status(ConnState.DISCONNECTED, t("net.crashed")))

    async def _disconnect(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.done():
            session.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session
        if self.status.state is not ConnState.DISCONNECTED:
            self._emit(Status(ConnState.DISCONNECTED))

    async def _reconnect(self) -> None:
        await self._disconnect()
        await self._connect()

    async def _handle(self, cmd: SendCommand) -> Ack:
        if self._agent is None:
            raise RuntimeError("no settings yet")
        return await self._agent.handle(cmd)

    async def _start_poller(self) -> None:
        self._poller = asyncio.create_task(self._poll_game())

    async def _poll_game(self) -> None:
        while True:
            backend = self._agent.backend if self._agent is not None else None
            if (game_state := getattr(backend, "game_state", None)) is not None:
                try:
                    state = game_state()
                except Exception:
                    log.exception("game window check failed")
                else:
                    if state is not self.game:
                        if self.game is GameState.CLOSED:
                            self._exe = None  # it may be another copy of the game this time
                        self._emit(Game(state))
                        if state is GameState.CLOSED:
                            self._addon_synced = None  # a new game session reads the file again
                    if state is GameState.ACTIVE:
                        await self._sync_addon()
                    await self._check_saved_names(backend, state)
            await asyncio.sleep(GAME_POLL_S)

    # ------------------------------------------------------------ names from the game
    def _names_wanted(self) -> bool:
        config = self.config
        return config is not None and config.game_context and config.mode == Mode.ADDON

    async def _configure_names(self, old: AgentConfig | None) -> None:
        if not self._names_wanted():
            if self.context is not None:
                await self._use_names(None)
            return
        if old is None or not (old.game_context and old.mode == Mode.ADDON):
            self._saved_mtime, self._saved_checked_at = None, -SAVED_NAMES_EVERY_S  # read the file again
        if not self.config.live_context:
            self._live_pause = 0.0

    async def _use_names(self, context: GameContext | None) -> None:
        """Keep these names unless older ones came in late; None forgets them, here and on the server."""
        if context is not None and self.context is not None and context.at < self.context.at:
            return
        self.context = context
        self._emit(Names.of(context))
        if self._agent is not None:
            await self._agent.send_context(context)

    async def _check_saved_names(self, backend: InputBackend | None, state: GameState) -> None:
        """Names from the addon's saved file, when the game wrote it again (/reload, logout)."""
        now = time.monotonic()
        if not self._names_wanted() or now - self._saved_checked_at < SAVED_NAMES_EVERY_S:
            return
        self._saved_checked_at = now
        try:
            if self._exe is None and state is not GameState.CLOSED:
                self._exe = await asyncio.to_thread(getattr(backend, "game_exe", lambda: None))
            mtime, context = await asyncio.to_thread(saved_names, self._exe, self._saved_mtime)
        except Exception:
            log.exception("reading names from the addon's file failed")
            return
        self._saved_mtime = mtime
        if context is not None:
            await self._use_names(context)

    def _start_live_names(self) -> None:
        """When a recording starts: copy fresh names from the game while the player speaks."""
        config, agent = self.config, self._agent
        if agent is None or not self._names_wanted() or not config.live_context:
            return
        if self._export is not None and not self._export.done():
            return
        now = time.monotonic()
        if now - self._live_at < max(LIVE_NAMES_EVERY_S, self._live_pause) or not self._game_focused():
            return
        self._live_at = now
        self._export = asyncio.create_task(self._live_names(agent))

    async def _live_names(self, agent: Agent) -> None:
        context = await agent.export_context()
        if context is None:
            low, high = LIVE_NAMES_BACKOFF_S
            self._live_pause = min(max(self._live_pause * 2, low), high)
            return
        self._live_pause = 0.0
        await self._use_names(context)

    async def _live_names_done(self) -> None:
        """Let the names copied at the start of a recording reach the server before the recording."""
        if (task := self._export) is not None and not task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), LIVE_NAMES_WAIT_S)

    # ------------------------------------------------------------ push-to-talk
    def _game_focused(self) -> bool:
        backend = self._agent.backend if self._agent is not None else None
        try:
            return backend is not None and backend.is_game_focused()
        except Exception:
            log.exception("game window check failed")
            return False

    async def _start_devices(self) -> None:
        loop = self._loop

        def on_press(press: Press) -> None:
            loop.call_soon_threadsafe(self._on_press, press)

        def on_pad(pad: Pad | None) -> None:
            state = PadState(True, pad.name, pad.playstation) if pad else PadState(False)
            loop.call_soon_threadsafe(self._emit, state)

        def on_level(level: float) -> None:
            loop.call_soon_threadsafe(self._emit, MicLevel(round(level, 3)))

        devices = self.make_devices(on_press, on_pad, on_level)
        if await asyncio.to_thread(devices.start):
            self.devices = devices
        else:
            self._emit(PadState(False, error=devices.error or t("pad.sdl_failed")))

    def _on_press(self, press: Press) -> None:
        if self._capture is not None and press.down and not self._capture.done():
            self._capture.set_result(press)
            return
        config = self.config
        if config is None or not config.ptt_enabled or press.button != config.ptt_button:
            return
        now = time.monotonic()
        cmd = self._ptt.press(now, self._game_focused()) if press.down else self._ptt.release(now)
        if cmd is not None:
            self._loop.create_task(self._finish(cmd))

    async def _finish(self, cmd: Cmd | None) -> None:
        """Carry out what the push-to-talk rules decided."""
        config, devices = self.config, self.devices
        if cmd is None or config is None or devices is None:
            return
        sounds = config.ptt_sounds
        match cmd:
            case Cmd.START:
                self._mic_test = False
                try:
                    await asyncio.wrap_future(devices.start_recording(config.mic_device))
                except Exception as e:
                    log.error("cannot record: %s", e)
                    self._ptt.abort()
                    if sounds:
                        devices.beep("error")
                    self._emit(VoiceResult("", "error", detail=t("mic.failed", error=e)))
                    return
                if sounds:
                    devices.beep("start")
                self._emit(Recording(RecState.RECORDING, time.time(), config.max_record_s))
                self._start_live_names()
                if self._ticker is None or self._ticker.done():
                    self._ticker = asyncio.create_task(self._tick())
            case Cmd.CANCEL:
                await asyncio.wrap_future(devices.stop_recording())
                if sounds:
                    devices.beep("cancel")
                self._emit(Recording(RecState.PROCESSING if self._sending else RecState.IDLE))
            case Cmd.SEND:
                pcm = await asyncio.wrap_future(devices.stop_recording())
                if sounds:
                    devices.beep("stop")
                asyncio.create_task(self._send_voice(pcm))

    async def _tick(self) -> None:
        while self._ptt.recording:
            await asyncio.sleep(RECORD_TICK_S)
            await self._finish(self._ptt.tick(time.monotonic()))

    async def _send_voice(self, pcm: bytes) -> None:
        agent = self._agent
        self._sending += 1
        if not self._ptt.recording:
            self._emit(Recording(RecState.PROCESSING))
        try:
            if agent is None:
                result = VoiceResult("", "offline", detail=t("net.offline"))
                self._emit(result)
            else:
                await self._live_names_done()
                result = await agent.send_voice(pcm)
        finally:
            self._sending -= 1
        if not self._ptt.recording:
            self._emit(Recording(RecState.PROCESSING if self._sending else RecState.IDLE))
        if result.status != "ok" and self.config is not None and self.devices is not None:
            if self.config.ptt_sounds:
                self.devices.beep("error")
        # In the game the badge waits for the message; tell it when none will come.
        if agent is not None and result.status not in ("ok", "not_focused"):
            await agent.type_control("status", result.status, badge_text(result.status))

    async def _set_mic_test(self, on: bool, device: str) -> None:
        if self.devices is None or self._ptt.recording:
            return
        if on:
            await asyncio.wrap_future(self.devices.start_recording(device, keep=False))
            self._mic_test = True
        elif self._mic_test:
            self._mic_test = False
            await asyncio.wrap_future(self.devices.stop_recording())

    async def _cancel_capture(self) -> None:
        if self._capture is not None and not self._capture.done():
            self._capture.set_result(None)

    async def _capture_press(self, timeout: float) -> Press | None:
        self._capture = self._loop.create_future()
        try:
            async with asyncio.timeout(timeout):
                return await self._capture
        except TimeoutError:
            return None
        finally:
            self._capture = None

    # ------------------------------------------------------------ addon setup
    async def _sync_addon(self) -> None:
        """Tell the addon a changed mode or time limit, so its badge follows the same rules."""
        config, agent, backend = self.config, self._agent, self._agent.backend if self._agent else None
        if config is None or agent is None or config.mode != Mode.ADDON or not config.ptt_enabled:
            return
        if self._ptt.recording or (self._bind_task is not None and not self._bind_task.done()):
            return
        wanted = (str(config.ptt_mode), float(config.max_record_s))
        if self._addon_synced == wanted:
            return
        if self._addon_synced == ADDON_NOT_SET_UP and time.monotonic() - self._addon_read_at < ADDON_REREAD_S:
            return
        if self._addon_synced in (None, ADDON_NOT_SET_UP):  # what does the addon have in this game session?
            self._addon_read_at = time.monotonic()
            db = await asyncio.to_thread(read_addon_db, backend)
            ptt = db.get("ptt") if isinstance(db.get("ptt"), dict) else {}
            if not ptt.get("key"):
                # No button in the game yet: nothing to keep in step until the check sets it up,
                # and the check sends the mode and the time limit itself.
                self._addon_synced = ADDON_NOT_SET_UP
                return
            self._addon_synced = (str(ptt.get("mode", "")), float(ptt.get("max", 0) or 0))
            if self._addon_synced == wanted:
                return
        ack = await agent.type_control("ptt", "config", f"{wanted[0]} {wanted[1]:g}")
        if ack["status"] == "ok":
            self._addon_synced = wanted

    async def _start_bind_check(self) -> None:
        await self._cancel_bind_check()
        self._bind_task = asyncio.create_task(self._bind_check())

    async def _cancel_bind_check(self) -> None:
        task, self._bind_task = self._bind_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _bind_check(self) -> None:
        config, agent = self.config, self._agent
        if config is None or agent is None:
            self._emit(BindCheck("error", error="not_connected", detail=t("bind.d_save_first")))
            return
        if config.mode != Mode.ADDON:
            self._emit(BindCheck("error", error="chat_mode", detail=t("bind.d_chat_mode")))
            return
        backend = agent.backend
        try:
            async with asyncio.timeout(BIND_WAIT_GAME_S):
                self._emit(BindCheck("waiting_game"))
                while self.game is GameState.CLOSED:
                    await asyncio.sleep(0.5)
                self._emit(BindCheck("waiting_focus"))
                while not self._game_focused():
                    await asyncio.sleep(0.3)
        except TimeoutError:
            self._emit(BindCheck("error", error="timeout", detail=t("bind.d_no_game")))
            return

        exe = await asyncio.to_thread(getattr(backend, "game_exe", lambda: None))
        nonce = secrets.token_hex(4)
        ack = await agent.type_control("ptt", "setup", f"{config.ptt_mode} {config.max_record_s:g} {nonce}")
        if ack["status"] != "ok":
            self._emit(BindCheck("error", error="timeout", detail=t("bind.d_not_focused")))
            return
        self._emit(BindCheck("sent"))

        pressed: Press | None = None
        try:
            async with asyncio.timeout(BIND_WAIT_ADDON_S):
                while True:
                    if pressed or self.recording.state is not RecState.IDLE:
                        await asyncio.sleep(0.5)
                    elif (pressed := await self._pressed_soon()) is not None:
                        self._emit(BindCheck("reloading"))
                    db = await asyncio.to_thread(read_addon_db_from, exe)
                    ptt = db.get("ptt") if isinstance(db.get("ptt"), dict) else {}
                    if ptt.get("nonce") == nonce:
                        self._addon_synced = (str(config.ptt_mode), float(config.max_record_s))
                        self._emit(self._bind_result(config, pressed, ptt))
                        return
        except TimeoutError:
            pass
        if exe is None:
            error, detail = "no_game_folder", t("bind.d_no_folder")
        elif not pressed:
            error, detail = "addon_missing", t("bind.d_addon_missing")
        else:
            error, detail = "timeout", t("bind.d_no_result")
        self._emit(BindCheck("error", error=error, detail=detail))

    @staticmethod
    def _bind_result(config: AgentConfig, pressed: Press | None, ptt: dict[str, Any]) -> BindCheck:
        key, previous = str(ptt.get("key", "")), str(ptt.get("previous", ""))
        if pressed is not None and pressed.button != config.ptt_button:
            # The addon took the button pressed in the game; the agent records on another one.
            playstation = pressed.pad.playstation
            return BindCheck(
                "error",
                key=key,
                error="wrong_button",
                detail=t(
                    "bind.d_wrong_button",
                    pressed=button_label(pressed.button, playstation),
                    wanted=button_label(config.ptt_button, playstation),
                ),
            )
        return BindCheck(
            "done",
            key=key,
            previous=previous,
            previous_name=str(ptt.get("previousName", "")) or previous,
            taken=bool(key),
        )

    async def _pressed_soon(self) -> Press | None:
        """Wait up to a second for any pad press (the player presses the button in the game)."""
        if self.devices is None:
            await asyncio.sleep(1)
            return None
        return await self._capture_press(1.0)

    async def _stop_all(self) -> None:
        await self._cancel_bind_check()
        await self._finish(self._ptt.abort())
        if self._ticker is not None:
            self._ticker.cancel()
        if self._export is not None:
            self._export.cancel()
        await self._disconnect()
        if self._poller is not None:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller


def read_addon_db_from(exe: Path | None) -> dict[str, Any]:
    """VoiceForeverDB from the newest SavedVariables file of the running game, {} if none."""
    if exe is None:
        return {}
    for path in savedvars.addon_files(exe):
        try:
            return savedvars.read_addon_db(path)
        except (OSError, savedvars.LuaError) as e:
            log.warning("cannot read %s: %s", path, e)
    return {}


def saved_names(exe: Path | None, seen_mtime: float | None) -> tuple[float | None, GameContext | None]:
    """The newest names in the addon's saved files, if they changed since `seen_mtime`.

    Returns the time the newest file was written, and its names if they are new.
    """
    if exe is None:
        return seen_mtime, None
    try:
        files = savedvars.addon_files(exe)
        mtime = files[0].stat().st_mtime if files else None
    except OSError:
        return seen_mtime, None
    if mtime is None or mtime == seen_mtime:
        return seen_mtime, None
    return mtime, from_saved(read_addon_db_from(exe))


def read_addon_db(backend: InputBackend | None) -> dict[str, Any]:
    exe = getattr(backend, "game_exe", None)
    return read_addon_db_from(exe() if exe is not None else None)
