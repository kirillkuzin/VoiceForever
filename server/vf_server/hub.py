"""WebSocket hub: desktop agents connect here and receive messages to type into WoW.

Protocol (JSON text frames):
  agent -> server  {"type": "hello", "version": "0.2.0", "mode": "addon" | "chat"}
  server -> agent  {"type": "send", "id": "...", "channel": "group", "target": "", "parts": ["..."]}
  agent -> server  {"type": "ack", "id": "...", "status": "ok" | "not_focused" | "error", "detail": "..."}
Push-to-talk from the agent (one binary frame: a JSON header line, "\n", then the WAV file):
  agent -> server  {"type": "voice", "id": "...", "format": "wav", "duration_ms": 2300}\n<RIFF...>
  server -> agent  {"type": "voice_result", "id": "...", "status": "ok" | "skip" | "empty" | ..., ...}
The server types the recognised message through the usual "send" before the voice_result.
Names and channels from the game (see gamectx.py), sent again when they change:
  agent -> server  {"type": "context", "v": 1, "source": "live" | "file", "age_s": 3, "channels": [...], ...}
  agent -> server  {"type": "context_clear"}  (the player turned it off)
The agent authenticates with "Authorization: Bearer <agent token>".
"""

import asyncio
import contextlib
import hmac
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aiohttp import WSMsgType, web

from .chat import ChatMessage

log = logging.getLogger(__name__)


class AckStatus(StrEnum):
    OK = "ok"
    NOT_FOCUSED = "not_focused"
    ERROR = "error"
    OFFLINE = "offline"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class Ack:
    status: AckStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is AckStatus.OK


@dataclass(frozen=True, slots=True)
class AgentInfo:
    version: str = "?"
    mode: str = "?"


@dataclass(frozen=True, slots=True)
class VoiceUpload:
    """A push-to-talk recording sent by the agent."""

    id: str
    format: str
    audio: bytes
    duration_ms: int = 0


# Turns a recording into the voice_result fields (status, text, ...). Set by main.
type VoiceHandler = Callable[[int, VoiceUpload], Awaitable[dict[str, Any]]]
# Takes the names and channels from the player's game (None: forget them). Set by main.
type ContextHandler = Callable[[int, dict[str, Any] | None], None]

MAX_HEADER_BYTES = 1024


def parse_voice_frame(frame: bytes) -> VoiceUpload:
    """Split a binary frame into its JSON header and the audio after the first newline."""
    header, sep, audio = frame.partition(b"\n")
    if not sep or len(header) > MAX_HEADER_BYTES:
        raise ValueError("voice frame must start with a JSON header line")
    match json.loads(header):
        case {"type": "voice", "id": str(voice_id), "format": str(fmt), **rest} if (
            0 < len(voice_id) <= 64
        ):
            duration = rest.get("duration_ms", 0)
            duration = duration if isinstance(duration, int) and not isinstance(duration, bool) else 0
            return VoiceUpload(voice_id, fmt.lower(), audio, max(duration, 0))
        case _:
            raise ValueError("voice header needs type, id and format")


class AgentHub:
    def __init__(
        self,
        users: Mapping[int, str],
        ack_timeout_s: float = 30.0,
        max_voice_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._tokens = tuple(users.items())  # (user_id, token)
        self._conns: dict[int, web.WebSocketResponse] = {}
        self._info: dict[int, AgentInfo] = {}
        self._pending: dict[str, asyncio.Future[Ack]] = {}
        self.ack_timeout_s = ack_timeout_s
        self.max_voice_bytes = max_voice_bytes
        self.voice_handler: VoiceHandler | None = None
        self.context_handler: ContextHandler | None = None
        # One recording at a time per player, in the order they were spoken.
        self._voice_locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ http
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/health", self._health)
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "agents_online": len(self._conns)})

    def _user_for_token(self, token: str) -> int | None:
        # Compare against every token so timing does not reveal which one matched.
        found = None
        for user_id, expected in self._tokens:
            if hmac.compare_digest(token.encode(), expected.encode()):
                found = user_id
        return found

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        user_id = self._user_for_token(token.strip()) if scheme.lower() == "bearer" else None
        if user_id is None:
            log.warning("agent auth failed from %s", request.remote)
            raise web.HTTPUnauthorized(text="bad agent token")

        ws = web.WebSocketResponse(
            heartbeat=20, max_msg_size=self.max_voice_bytes + MAX_HEADER_BYTES + 1
        )
        await ws.prepare(request)
        if (old := self._conns.get(user_id)) is not None and not old.closed:
            await old.close(message=b"replaced by a newer connection")
        self._conns[user_id] = ws
        self._info[user_id] = AgentInfo()
        log.info("agent connected for user %s", user_id)

        # Voice is handled in tasks: the loop below must keep reading, because the
        # message typed for a recording is confirmed by an ack on this same connection.
        voice_tasks: set[asyncio.Task[None]] = set()
        try:
            async for frame in ws:
                if frame.type is WSMsgType.BINARY:
                    task = asyncio.create_task(self._voice(user_id, ws, frame.data))
                    voice_tasks.add(task)
                    task.add_done_callback(voice_tasks.discard)
                    continue
                if frame.type is not WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(frame.data)
                except json.JSONDecodeError:
                    continue
                match data:
                    case {"type": "hello", **rest}:
                        self._info[user_id] = AgentInfo(
                            str(rest.get("version", "?")), str(rest.get("mode", "?"))
                        )
                    case {"type": "ack", "id": str(msg_id), **rest}:
                        fut = self._pending.get(msg_id)
                        if fut is not None and not fut.done():
                            try:
                                status = AckStatus(rest.get("status", "error"))
                            except ValueError:
                                status = AckStatus.ERROR
                            fut.set_result(Ack(status, str(rest.get("detail", ""))))
                    case {"type": "context" | "context_clear" as kind} if self.context_handler:
                        # Handled before the next frame: a recording sent after it already uses it.
                        try:
                            self.context_handler(user_id, data if kind == "context" else None)
                        except Exception:
                            log.exception("bad names from the game of user %s", user_id)
        finally:
            for task in voice_tasks:
                task.cancel()
            if self._conns.get(user_id) is ws:
                del self._conns[user_id]
                self._info.pop(user_id, None)
            log.info("agent disconnected for user %s", user_id)
        return ws

    async def _voice(self, user_id: int, ws: web.WebSocketResponse, frame: bytes) -> None:
        try:
            upload = parse_voice_frame(frame)
        except ValueError as e:
            log.warning("bad voice frame from user %s: %s", user_id, e)
            return
        if self.voice_handler is None:
            result: dict[str, Any] = {"status": "unsupported", "detail": "this server does not take voice from the agent"}
        elif len(upload.audio) > self.max_voice_bytes:
            result = {"status": "too_long", "detail": "the recording is too long"}
        else:
            lock = self._voice_locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                try:
                    result = await self.voice_handler(user_id, upload)
                except Exception:
                    log.exception("voice from user %s failed", user_id)
                    result = {"status": "error", "detail": "server error"}
        with contextlib.suppress(ConnectionResetError):
            if not ws.closed:
                await ws.send_json({"type": "voice_result", "id": upload.id, **result})

    # ------------------------------------------------------------------ api
    def is_online(self, user_id: int) -> bool:
        ws = self._conns.get(user_id)
        return ws is not None and not ws.closed

    def info(self, user_id: int) -> AgentInfo | None:
        return self._info.get(user_id) if self.is_online(user_id) else None

    async def send(
        self, user_id: int, msg: ChatMessage, parts: list[str], voice_id: str = ""
    ) -> Ack:
        ws = self._conns.get(user_id)
        if ws is None or ws.closed:
            return Ack(AckStatus.OFFLINE)
        msg_id = uuid.uuid4().hex
        fut: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            async with asyncio.timeout(self.ack_timeout_s):
                command = {
                    "type": "send",
                    "id": msg_id,
                    "channel": msg.channel,
                    "target": msg.target,
                    "parts": parts,
                }
                if voice_id:  # lets the agent window tie the typed message to the recording
                    command["voice_id"] = voice_id
                await ws.send_json(command)
                return await fut
        except TimeoutError:
            return Ack(AckStatus.TIMEOUT)
        except ConnectionResetError:
            return Ack(AckStatus.OFFLINE)
        finally:
            self._pending.pop(msg_id, None)
