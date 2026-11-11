"""WebSocket client: keeps a connection to the server and types received messages."""

import asyncio
import json
import logging
from typing import Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from . import __version__
from .config import AgentConfig
from .plan import Action, SendCommand, build_actions

log = logging.getLogger("vf_agent")

type Ack = dict[str, str]


class InputBackend(Protocol):
    def is_game_focused(self) -> bool: ...
    def run(self, actions: list[Action]) -> None: ...


class AuthError(RuntimeError):
    pass


def ack(cmd: SendCommand, status: str, detail: str = "") -> Ack:
    return {"type": "ack", "id": cmd.id, "status": status, "detail": detail}


class Agent:
    def __init__(self, config: AgentConfig, backend: InputBackend) -> None:
        self.config = config
        self.backend = backend
        self._typing = asyncio.Lock()  # one message at a time, in arrival order

    async def wait_for_focus(self) -> bool:
        try:
            async with asyncio.timeout(self.config.focus_wait_s):
                while not self.backend.is_game_focused():
                    await asyncio.sleep(0.2)
        except TimeoutError:
            return False
        return True

    async def handle(self, cmd: SendCommand) -> Ack:
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
        headers = {"Authorization": f"Bearer {self.config.token}"}
        delay = 1.0
        while True:
            try:
                async with connect(
                    self.config.server_url,
                    additional_headers=headers,
                    ping_interval=20,
                    open_timeout=15,
                ) as ws:
                    log.info("connected to %s", self.config.server_url)
                    delay = 1.0
                    await self.serve(ws)
                log.warning("server closed the connection")
            except InvalidStatus as e:
                if e.response.status_code in (401, 403):
                    raise AuthError("сервер не принял токен агента") from e
                log.warning("server answered HTTP %s", e.response.status_code)
            except (OSError, TimeoutError, WebSocketException) as e:
                log.warning("connection lost: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
