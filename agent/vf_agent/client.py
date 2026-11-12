"""WebSocket client: keeps a connection to the server and types received messages.

Everything the window shows comes from here as events: the connection Status, a Delivery
for every message the server asked to type, and the GameState of the game window.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import socket
import ssl
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
    WebSocketException,
)

from . import __version__, audio
from .config import AgentConfig
from .context import GameContext, parse_export
from .i18n import t
from .plan import Action, GameState, Kind, Mode, SendCommand, build_actions, export_actions

log = logging.getLogger("vf_agent")

type Ack = dict[str, str]

OPEN_TIMEOUT_S = 15
# Upload, speech recognition, Claude and typing; the server gives typing up to 30 s itself.
VOICE_TIMEOUT_S = 90
MAX_RETRY_DELAY_S = 30.0
# Close reason the server hub sends when another agent connects with the same token.
REPLACED_REASON = "replaced by a newer connection"
# Close code the server sends when the token was revoked on the account page.
REVOKED_CODE = 4001


class InputBackend(Protocol):
    def is_game_focused(self) -> bool: ...
    def run(self, actions: list[Action]) -> str | None: ...  # what a COPY action found


class ConnState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"  # waiting before the next attempt
    AUTH_FAILED = "auth_failed"  # the server rejected or revoked the token; no retries
    REPLACED = "replaced"  # another agent took over this token; no retries


@dataclass(frozen=True, slots=True)
class Status:
    state: ConnState
    detail: str = ""  # why, in words for the window
    attempt: int = 0  # failed attempts in a row
    retry_at: float | None = None  # time.time() of the next attempt
    since: float | None = None  # time.time() when the connection opened
    link: str = ""  # the account page, when the server gives one with a refused token


@dataclass(frozen=True, slots=True)
class Delivery:
    """A message the server asked to type, and what happened to it."""

    channel: str
    target: str
    text: str
    status: str  # "ok" | "not_focused" | "error"
    detail: str = ""
    at: float = field(default_factory=time.time)
    voice_id: str = ""  # the push-to-talk recording it came from, if any


@dataclass(frozen=True, slots=True)
class Game:
    state: GameState


@dataclass(frozen=True, slots=True)
class VoiceResult:
    """What the server did with a push-to-talk recording.

    status: ok | not_focused | error | timeout | offline (typing, as for a Delivery), or
    skip (not meant for chat) | empty | stt_error | bad_audio | too_long | unsupported, or
    denied (the account does not allow it: see reason and link).
    """

    id: str
    status: str
    channel: str = ""
    target: str = ""
    label: str = ""  # the server's channel name for people, e.g. "Guild" or "Whisper → Arthorn"
    text: str = ""  # what was typed (or would have been)
    transcript: str = ""  # what speech recognition heard
    detail: str = ""
    at: float = field(default_factory=time.time)
    reason: str = ""  # why it was denied: unknown | revoked | no_access | limit | rate | unavailable
    link: str = ""  # where to fix that: the account page
    note: str = ""  # why the keyword rules picked the channel, e.g. ai_limit
    ai: bool = False  # Claude picked the channel

    @classmethod
    def from_json(cls, voice_id: str, data: dict[str, Any]) -> VoiceResult:
        def text(key: str) -> str:
            value = data.get(key, "")
            return value if isinstance(value, str) else ""

        return cls(voice_id, text("status") or "error", text("channel"), text("target"), text("label"),
                   text("text"), text("transcript"), text("detail"), reason=text("reason"),
                   link=web_link(text("link")), note=text("note"), ai=data.get("ai") is True)


@dataclass(frozen=True, slots=True)
class Account:
    """What the server's account service says about the player, for the status tab."""

    plan: str = ""
    ai_left: int | None = None  # AI messages left today
    ai_limit: int | None = None
    until: float | None = None  # paid until, time.time()
    link: str = ""  # the account page

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Account:
        def number(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        plan = data.get("plan")
        return cls(plan if isinstance(plan, str) else "", number("ai_left"), number("ai_limit"),
                   number("until") or None, web_link(data.get("link")))


def web_link(value: object) -> str:
    """Only https links from the server reach a button."""
    return value if isinstance(value, str) and value.startswith("https://") and len(value) < 500 else ""


class RecState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"  # sent to the server, waiting for the result


@dataclass(frozen=True, slots=True)
class Recording:
    state: RecState
    started_at: float | None = None  # time.time() when recording began, for the timer
    max_s: float = 0.0


@dataclass(frozen=True, slots=True)
class PadState:
    connected: bool
    name: str = ""
    playstation: bool = False
    error: str = ""  # why the pad and microphone cannot work at all (no SDL3)


@dataclass(frozen=True, slots=True)
class MicLevel:
    level: float  # 0..1, about 20 times a second while the microphone is open


@dataclass(frozen=True, slots=True)
class BindCheck:
    """A step of the in-game check of the push-to-talk button.

    step: waiting_game -> waiting_focus -> sent -> reloading -> done, or error.
    """

    step: str
    key: str = ""  # the game's name for the button the addon took, e.g. PADRSTICK
    previous: str = ""  # what the game had on that button ("" = nothing), e.g. TOGGLEPINGLISTENER
    previous_name: str = ""  # the same as the game's key bindings window shows it
    taken: bool = False
    error: str = ""  # addon_missing | timeout | chat_mode | not_connected | no_game_folder | wrong_button
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Names:
    """The names and channels from the game the server has now, counted for the window."""

    source: str = ""  # "live" | "file" | "" (none yet, or sending them is off)
    at: float | None = None  # time.time() when the game made the snapshot
    me: str = ""  # "Name-Realm" of the character
    counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, context: GameContext | None) -> Names:
        if context is None:
            return cls()
        return cls(str(context.source), context.at, context.data["me"], context.counts())


type Event = Status | Delivery | Game | VoiceResult | Recording | PadState | MicLevel | BindCheck | Names | Account
type Listener = Callable[[Event], None]


class AuthError(RuntimeError):
    pass


def refusal(error: InvalidStatus) -> tuple[str, str]:
    """The server's reason and account link for a refused token, from the 401 body."""
    try:
        data = json.loads(error.response.body or b"{}")
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    reason = data.get("error")
    return reason if isinstance(reason, str) else "", web_link(data.get("link"))


def auth_detail(reason: str, error: InvalidStatus) -> str:
    return t("net.revoked") if reason == "revoked" else describe(error)


class ReplacedError(RuntimeError):
    pass


def context_message(context: GameContext | None) -> dict[str, Any]:
    return context.message() if context is not None else {"type": "context_clear"}


def ack(cmd: SendCommand, status: str, detail: str = "") -> Ack:
    return {"type": "ack", "id": cmd.id, "status": status, "detail": detail}


def describe(error: BaseException) -> str:
    """Short reason for a failed connection, for the window and the log."""
    match error:
        case InvalidStatus():
            return t("net.http", code=error.response.status_code)
        case InvalidURI():
            return t("net.bad_uri")
        case InvalidHandshake():
            return t("net.not_websocket")
        case ConnectionClosed():
            return t("net.closed")
        case TimeoutError():
            return t("net.timeout", s=OPEN_TIMEOUT_S)
        case socket.gaierror():
            return t("net.not_found")
        case ssl.SSLCertVerificationError():
            return t("net.certificate", error=error.verify_message)
        case ssl.SSLError():
            return t("net.tls", error=error.reason or error)
        case ConnectionRefusedError():
            return t("net.refused")
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
        self._ws: ClientConnection | None = None
        self._voice_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.context: GameContext | None = None  # sent again after every reconnect
        self.account_link = ""  # the last account page the server named

    def _emit(self, event: Event) -> None:
        if isinstance(event, Status):
            self.status = event
        if isinstance(event, Account) and event.link:
            self.account_link = event.link
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
        self._emit(Delivery(cmd.channel, cmd.target, " ".join(cmd.parts), result["status"],
                            result["detail"], voice_id=cmd.voice_id))
        return result

    async def type_control(self, channel: str, target: str, text: str) -> Ack:
        """Type a line for the addon itself (push-to-talk setup, in-game badge). Addon mode only."""
        if self.config.mode != Mode.ADDON:
            return {"type": "ack", "id": "", "status": "error", "detail": t("net.needs_addon")}
        cmd = SendCommand(f"{channel}-{uuid.uuid4().hex[:8]}", channel, target, (text,))
        return await self._type(cmd)

    async def send_voice(self, pcm: bytes) -> VoiceResult:
        """Send a recording to the server and wait until its message is typed (or refused)."""
        voice_id = uuid.uuid4().hex[:16]
        ws = self._ws
        if ws is None:
            result = VoiceResult(voice_id, "offline", detail=t("net.offline"))
        else:
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._voice_waiters[voice_id] = future
            frame = audio.voice_frame(voice_id, audio.encode_wav(pcm), audio.duration_ms(pcm))
            try:
                await ws.send(frame)
                async with asyncio.timeout(VOICE_TIMEOUT_S):
                    result = VoiceResult.from_json(voice_id, await future)
            except ConnectionClosed:
                result = VoiceResult(voice_id, "offline", detail=t("net.dropped"))
            except TimeoutError:
                result = VoiceResult(voice_id, "timeout", detail=t("net.voice_timeout"))
            finally:
                self._voice_waiters.pop(voice_id, None)
        log.info("voice %s: %s %s", voice_id, result.status, result.detail)
        self._emit(result)
        return result

    async def export_context(self) -> GameContext | None:
        """Copy the names and channels the addon has right now, if the game is in front.

        None when the copy did not work: the game lost focus, or an addon without the export
        did not answer (then nothing in the game has focus, so the agent presses nothing more).
        """
        if self.config.mode != Mode.ADDON:
            return None
        nonce = secrets.token_hex(4)
        actions = export_actions(self.config.addon_hotkey, nonce, self.config.open_delay_ms)
        async with self._typing:
            if not self.backend.is_game_focused():
                return None
            try:
                copied = await asyncio.to_thread(self.backend.run, actions)
            except Exception:
                log.exception("copying names from the game failed")
                return None
            context = parse_export(copied, nonce)
            if context is not None:  # the addon's box holds the snapshot: Enter empties and closes it
                try:
                    await asyncio.to_thread(self.backend.run, [Action(Kind.KEY, "enter")])
                except Exception:
                    log.exception("closing the addon's box failed")
        if context is None:
            log.info("no names from the game: the addon did not answer")
        else:
            log.info("names from the game: %s", context.counts())
        return context

    async def send_context(self, context: GameContext | None) -> None:
        """Give the server these names and channels (None: forget them); kept for reconnects."""
        self.context = context
        if (ws := self._ws) is not None:
            with contextlib.suppress(ConnectionClosed):
                await ws.send(json.dumps(context_message(context), ensure_ascii=False))

    async def _type(self, cmd: SendCommand) -> Ack:
        if not cmd.parts:
            return ack(cmd, "error", t("net.empty"))
        try:
            actions = build_actions(
                cmd,
                self.config.mode,
                self.config.addon_hotkey,
                self.config.open_delay_ms,
                self.config.part_delay_ms,
            )
        except ValueError as e:
            return ack(cmd, "error", str(e))
        async with self._typing:
            if not await self.wait_for_focus():
                return ack(cmd, "not_focused")
            try:
                await asyncio.to_thread(self.backend.run, actions)
            except Exception as e:  # report any input failure back to Telegram
                log.exception("typing failed")
                return ack(cmd, "error", str(e)[:200])
        log.info("sent %d part(s) to %s", len(cmd.parts), cmd.channel)
        return ack(cmd, "ok")

    async def serve(self, ws: ClientConnection) -> None:
        await ws.send(json.dumps({"type": "hello", "version": __version__, "mode": self.config.mode}))
        self._ws = ws
        closed: ConnectionClosed | None = None
        try:
            if self.context is not None:
                await ws.send(json.dumps(context_message(self.context), ensure_ascii=False))
            async with asyncio.TaskGroup() as tasks:
                try:
                    async for raw in ws:
                        try:
                            match json.loads(raw):
                                case {"type": "send", **data}:
                                    cmd = SendCommand.from_json(data)
                                case {"type": "voice_result", "id": str(voice_id), **data}:
                                    if (waiter := self._voice_waiters.get(voice_id)) and not waiter.done():
                                        waiter.set_result(data)
                                    continue
                                case {"type": "account", **data}:
                                    self._emit(Account.from_json(data))
                                    continue
                                case _:
                                    continue
                        except ValueError as e:  # bad JSON or bad command
                            log.warning("bad message from server: %s", e)
                            continue
                        tasks.create_task(self._reply(ws, cmd))
                except ConnectionClosed as e:  # not a clean close: raised below, not in a TaskGroup error group
                    closed = e
        finally:
            self._ws = None
            for waiter in self._voice_waiters.values():
                if not waiter.done():
                    waiter.set_exception(ConnectionClosed(None, None))
        if closed is not None:
            raise closed

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
                        try:
                            await self.serve(ws)
                        except ConnectionClosed:
                            if ws.close_code != REVOKED_CODE:
                                raise
                    if ws.close_reason == REPLACED_REASON:
                        log.warning("another agent connected with this token")
                        self._emit(Status(ConnState.REPLACED, t("net.replaced")))
                        raise ReplacedError(t("net.replaced"))
                    if ws.close_code == REVOKED_CODE:
                        log.warning("the token was revoked")
                        self._emit(Status(ConnState.AUTH_FAILED, t("net.revoked"), link=self.account_link))
                        raise AuthError(t("net.revoked"))
                    reason = t("net.server_closed")
                except InvalidStatus as e:
                    if e.response.status_code in (401, 403):
                        why, link = refusal(e)
                        self._emit(Status(ConnState.AUTH_FAILED, auth_detail(why, e), link=link))
                        raise AuthError(t("net.revoked") if why == "revoked" else t("net.auth")) from e
                    reason = t("net.access_down") if e.response.status_code == 503 else describe(e)
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
