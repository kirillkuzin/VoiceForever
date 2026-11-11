"""WebSocket hub: desktop agents connect here and receive messages to type into WoW.

Protocol (JSON text frames):
  agent -> server  {"type": "hello", "version": "0.2.0", "mode": "addon" | "chat"}
  server -> agent  {"type": "send", "id": "...", "channel": "group", "target": "", "parts": ["..."]}
  agent -> server  {"type": "ack", "id": "...", "status": "ok" | "not_focused" | "error", "detail": "..."}
The agent authenticates with "Authorization: Bearer <agent token>".
"""

import asyncio
import hmac
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

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


class AgentHub:
    def __init__(self, users: Mapping[int, str], ack_timeout_s: float = 30.0) -> None:
        self._tokens = tuple(users.items())  # (user_id, token)
        self._conns: dict[int, web.WebSocketResponse] = {}
        self._info: dict[int, AgentInfo] = {}
        self._pending: dict[str, asyncio.Future[Ack]] = {}
        self.ack_timeout_s = ack_timeout_s

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

        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await ws.prepare(request)
        if (old := self._conns.get(user_id)) is not None and not old.closed:
            await old.close(message=b"replaced by a newer connection")
        self._conns[user_id] = ws
        self._info[user_id] = AgentInfo()
        log.info("agent connected for user %s", user_id)

        try:
            async for frame in ws:
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
        finally:
            if self._conns.get(user_id) is ws:
                del self._conns[user_id]
                self._info.pop(user_id, None)
            log.info("agent disconnected for user %s", user_id)
        return ws

    # ------------------------------------------------------------------ api
    def is_online(self, user_id: int) -> bool:
        ws = self._conns.get(user_id)
        return ws is not None and not ws.closed

    def info(self, user_id: int) -> AgentInfo | None:
        return self._info.get(user_id) if self.is_online(user_id) else None

    async def send(self, user_id: int, msg: ChatMessage, parts: list[str]) -> Ack:
        ws = self._conns.get(user_id)
        if ws is None or ws.closed:
            return Ack(AckStatus.OFFLINE)
        msg_id = uuid.uuid4().hex
        fut: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            async with asyncio.timeout(self.ack_timeout_s):
                await ws.send_json({
                    "type": "send",
                    "id": msg_id,
                    "channel": msg.channel,
                    "target": msg.target,
                    "parts": parts,
                })
                return await fut
        except TimeoutError:
            return Ack(AckStatus.TIMEOUT)
        except ConnectionResetError:
            return Ack(AckStatus.OFFLINE)
        finally:
            self._pending.pop(msg_id, None)
