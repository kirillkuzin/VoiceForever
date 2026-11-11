"""Runs the agent on its own asyncio loop thread, so the window and the tray can drive it.

Every public method is safe to call from any thread. Events reach `on_event` on the loop
thread, numbered by `seq`, so a window that asked for a snapshot can drop older events.
"""

import asyncio
import contextlib
import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from .client import (
    Ack,
    Agent,
    AuthError,
    ConnState,
    Delivery,
    Event,
    Game,
    InputBackend,
    ReplacedError,
    Status,
)
from .config import AgentConfig
from .plan import GameState, SendCommand

log = logging.getLogger("vf_agent")

type BackendFactory = Callable[[AgentConfig], InputBackend]
type SeqListener = Callable[[int, Event], None]

GAME_POLL_S = 1.0
HISTORY = 50


class Controller:
    def __init__(self, make_backend: BackendFactory, on_event: SeqListener | None = None) -> None:
        self.make_backend = make_backend
        self.on_event = on_event
        self.config: AgentConfig | None = None
        self.status = Status(ConnState.DISCONNECTED)
        self.game = GameState.CLOSED
        self.history: deque[Delivery] = deque(maxlen=HISTORY)
        self.seq = 0
        self._agent: Agent | None = None
        self._session: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None
        self._test_ids = itertools.count(1)
        self._lock = threading.Lock()  # guards the snapshot fields above against the window's reads
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="vf-agent", daemon=True)

    # ------------------------------------------------------------ thread-safe API
    def start(self) -> None:
        self._thread.start()
        self._call(self._start_poller)

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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seq": self.seq,
                "status": self.status,
                "game": self.game,
                "history": list(self.history),
            }

    def shutdown(self, timeout: float = 5.0) -> None:
        if not self._thread.is_alive():
            return
        with contextlib.suppress(Exception):
            self._call(self._stop_all).result(timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)

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
                case Delivery():
                    self.history.appendleft(event)
                case Game(state):
                    self.game = state
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
        backend = self.make_backend(config)
        if self._agent is None:
            self._agent = Agent(config, backend, self._emit)
            return False
        if (old.server_url, old.token, old.mode) == (config.server_url, config.token, config.mode):
            # Input settings apply to the next message; the connection stays as it is.
            self._agent.config, self._agent.backend = config, backend
            return False
        # A new token is the usual fix for a rejected one, so try again right away.
        retry = self.running or self.status.state in (ConnState.AUTH_FAILED, ConnState.REPLACED)
        await self._disconnect()
        self._agent = Agent(config, backend, self._emit)
        if retry:
            await self._connect()
        return retry

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
            self._emit(Status(ConnState.DISCONNECTED, "внутренняя ошибка агента, подробности в журнале"))

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
                        self._emit(Game(state))
            await asyncio.sleep(GAME_POLL_S)

    async def _stop_all(self) -> None:
        await self._disconnect()
        if self._poller is not None:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
