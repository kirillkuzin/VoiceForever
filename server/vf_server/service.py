"""The voice -> text -> route -> agent pipeline, independent of Telegram."""

import logging
from dataclasses import dataclass

from .chat import ChatMessage, split_message, validate
from .hub import Ack, AckStatus, AgentHub
from .router import LLMRouter, RouteContext, RouteResult, rule_route
from .store import Store
from .stt import Transcriber

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Delivery:
    ack: Ack
    message: ChatMessage
    parts: list[str]


class VoiceService:
    def __init__(
        self,
        store: Store,
        hub: AgentHub,
        transcriber: Transcriber,
        router: LLMRouter | None,
        max_message_bytes: int = 255,
        global_names: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.hub = hub
        self.transcriber = transcriber
        self.router = router
        self.max_message_bytes = max_message_bytes
        self.global_names = list(global_names)

    def context(self, user_id: int) -> RouteContext:
        s = self.store.get(user_id)
        return RouteContext(
            default_channel=s.default_channel,
            rewrite_style=s.rewrite_style,
            known_names=list(dict.fromkeys([*self.global_names, *s.names()])),
            recent=self.store.recent(user_id),
        )

    async def transcribe(self, user_id: int, audio: bytes, filename: str) -> str:
        names = self.context(user_id).known_names
        hint = f"Имена: {', '.join(names)}." if names else ""
        return await self.transcriber.transcribe(audio, filename, extra_prompt=hint)

    async def route(self, user_id: int, transcript: str) -> RouteResult:
        ctx = self.context(user_id)
        if self.router is None:
            return rule_route(transcript, ctx)
        return await self.router.route(transcript, ctx)

    async def deliver(self, user_id: int, message: ChatMessage) -> Delivery:
        default = self.store.get(user_id).default_channel
        message = validate(message.channel, message.target, message.text, default)
        if not (parts := split_message(message.text, self.max_message_bytes)):
            return Delivery(Ack(AckStatus.ERROR, "пустое сообщение"), message, [])
        ack = await self.hub.send(user_id, message, parts)
        if ack.ok:
            self.store.remember(user_id, message.channel, message.target, message.text)
        log.info("user %s -> %s: %s", user_id, message.channel, ack.status)
        return Delivery(ack, message, parts)
