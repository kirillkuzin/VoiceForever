"""WebSocket client: keeps a connection to the server and types received messages.

Everything the window shows comes from here as events: the connection Status, a Delivery
for every message the server asked to type, and the GameState of the game window.
"""

import asyncio
import json
import logging
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
    WebSocketException,
)

from . import __version__
from .config import AgentConfig
from .plan import Action, GameState, SendCommand, build_actions

log = logging.getLogger("vf_agent")

type Ack = dict[str, str]

OPEN_TIMEOUT_S = 15
MAX_RETRY_DELAY_S = 30.0
# Close reason the server hub sends when another agent connects with the same token.
REPLACED_REASON = "replaced by a newer connection"


class InputBackend(Protocol):
    def is_game_focused(self) -> bool: ...
    def run(self, actions: list[Action]) -> None: ...


class ConnState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"  # waiting before the next attempt
    AUTH_FAILED = "auth_failed"  # the server rejected the token; no retries
    REPLACED = "replaced"  # another agent took over this token; no retries


@dataclass(frozen=True, slots=True)
class Status:
    state: ConnState
    detail: str = ""  # why, in words for the window
    attempt: int = 0  # failed attempts in a row
    retry_at: float | None = None  # time.time() of the next attempt
    since: float | None = None  # time.time() when the connection opened


@dataclass(frozen=True, slots=True)
class Delivery:
    """A message the server asked to type, and what happened to it."""

    channel: str
    target: str
    text: str
    status: str  # "ok" | "not_focused" | "error"
    detail: str = ""
    at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class Game:
    state: GameState


type Event = Status | Delivery | Game
type Listener = Callable[[Event], None]


class AuthError(RuntimeError):
    pass


class ReplacedError(RuntimeError):
    pass


def ack(cmd: SendCommand, status: str, detail: str = "") -> Ack:
    return {"type": "ack", "id": cmd.id, "status": status, "detail": detail}


def describe(error: BaseException) -> str:
    """Short reason for a failed connection, for the window and the log."""
    match error:
        case InvalidStatus():
            return f"сервер ответил HTTP {error.response.status_code}"
        case InvalidURI():
            return "неверный адрес сервера"
        case InvalidHandshake():
            return "по этому адресу не WebSocket-сервер"
        case ConnectionClosed():
            return "соединение оборвалось"
        case TimeoutError():
            return f"сервер не ответил за {OPEN_TIMEOUT_S} с"
        case socket.gaierror():
            return "адрес сервера не найден"
        case ssl.SSLCertVerificationError():
            return f"сертификат сервера не прошёл проверку: {error.verify_message}"
        case ssl.SSLError():
            return f"ошибка TLS: {error.reason or error}"
        case ConnectionRefusedError():
            return "сервер отказал в подключении"
        case OSError() if error.strerror:
            return error.strerror
        case _:
            return str(error) or type(error).__name__


class Agent:
    def __init__(
        self, config: AgentConfig, backend: InputBackend, on_event: Listener | None = None
    ) -> None:
        self.config = config
        self.backend = backend
        self.on_event = on_event
        self.status = Status(ConnState.DISCONNECTED)
        self._typing = asyncio.Lock()  # one message at a time, in arrival order

    def _emit(self, event: Event) -> None:
        if isinstance(event, Status):
            self.status = event
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # a broken listener must not drop the connection
                log.exception("event listener failed")

    async def wait_for_focus(self) -> bool:
        try:
            async with asyncio.timeout(self.config.focus_wait_s):
                while not self.backend.is_game_focused():
                    await asyncio.sleep(0.2)
        except TimeoutError:
            return False
        return True

    async def handle(self, cmd: SendCommand) -> Ack:
        result = await self._type(cmd)
        self._emit(Delivery(cmd.channel, cmd.target, " ".join(cmd.parts), result["status"], result["detail"]))
        return result

    async def _type(self, cmd: SendCommand) -> Ack:
        if not cmd.parts:
            return ack(cmd, "error", "пустое сообщение")
        async with self._typing:
            if not await self.wait_for_focus():
                return ack(cmd, "not_focused")
            actions = build_actions(
                cmd,
                self.config.mode,
                self.config.addon_hotkey,
                self.config.open_delay_ms,
                self.config.part_delay_ms,
            )
            try:
                await asyncio.to_thread(self.backend.run, actions)
            except Exception as e:  # report any input failure back to Telegram
                log.exception("typing failed")
                return ack(cmd, "error", str(e)[:200])
        log.info("sent %d part(s) to %s", len(cmd.parts), cmd.channel)
        return ack(cmd, "ok")

    async def serve(self, ws: ClientConnection) -> None:
        await ws.send(json.dumps({"type": "hello", "version": __version__, "mode": self.config.mode}))
        async with asyncio.TaskGroup() as tasks:
            async for raw in ws:
                try:
                    match json.loads(raw):
                        case {"type": "send", **data}:
                            cmd = SendCommand.from_json(data)
                        case _:
                            continue
                except ValueError as e:  # bad JSON or bad command
                    log.warning("bad message from server: %s", e)
                    continue
                tasks.create_task(self._reply(ws, cmd))

    async def _reply(self, ws: ClientConnection, cmd: SendCommand) -> None:
        result = await self.handle(cmd)
        try:
            await ws.send(json.dumps(result, ensure_ascii=False))
        except ConnectionClosed:
            log.warning("could not send ack for %s: connection closed", cmd.id)

    async def run_forever(self) -> None:
        """Stay connected, reconnecting with growing pauses. Cancel the task to disconnect.

        Raises AuthError when the server rejects the token and ReplacedError when another
        agent connects with the same token: retrying would not help in either case.
        """
        headers = {"Authorization": f"Bearer {self.config.token}"}
        url = self.config.server_url
        delay = 1.0
        attempt = 0
        try:
            while True:
                self._emit(Status(ConnState.CONNECTING, attempt=attempt))
                try:
                    async with connect(
                        url, additional_headers=headers, ping_interval=20, open_timeout=OPEN_TIMEOUT_S
                    ) as ws:
                        log.info("connected to %s", url)
                        self._emit(Status(ConnState.CONNECTED, since=time.time()))
                        delay, attempt = 1.0, 0
                        await self.serve(ws)
                    if ws.close_reason == REPLACED_REASON:
                        log.warning("another agent connected with this token")
                        self._emit(Status(ConnState.REPLACED, "с этим токеном подключился другой агент"))
                        raise ReplacedError("с этим токеном подключился другой агент")
                    reason = "сервер закрыл соединение"
                except InvalidStatus as e:
                    if e.response.status_code in (401, 403):
                        self._emit(Status(ConnState.AUTH_FAILED, describe(e)))
                        raise AuthError("сервер не принял токен агента") from e
                    reason = describe(e)
                except (OSError, TimeoutError, WebSocketException) as e:
                    reason = describe(e)
                attempt += 1
                log.warning("connection lost: %s; next try in %.0f s", reason, delay)
                self._emit(Status(ConnState.RECONNECTING, reason, attempt, retry_at=time.time() + delay))
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_S)
        except asyncio.CancelledError:
            self._emit(Status(ConnState.DISCONNECTED))
            raise
