"""The voice -> text -> route -> agent pipeline, independent of Telegram."""

import io
import logging
import wave
from dataclasses import dataclass
from typing import Any

from .chat import ChatMessage, split_message, validate
from .hub import Ack, AckStatus, AgentHub, VoiceUpload
from .i18n import LANGUAGES, t
from .router import Decision, LLMRouter, RouteContext, RouteResult, rule_route
from .store import Store
from .stt import Transcriber, TranscriptionError

MIN_VOICE_MS = 300  # shorter clips are accidental presses

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
        max_voice_s: float = 60.0,
    ) -> None:
        self.store = store
        self.hub = hub
        self.transcriber = transcriber
        self.router = router
        self.max_message_bytes = max_message_bytes
        self.global_names = list(global_names)
        self.max_voice_s = max_voice_s

    def context(self, user_id: int) -> RouteContext:
        s = self.store.get(user_id)
        return RouteContext(
            default_channel=s.default_channel,
            rewrite_style=s.rewrite_style,
            known_names=list(dict.fromkeys([*self.global_names, *s.names()])),
            recent=self.store.recent(user_id),
        )

    def speech_language(self, user_id: int) -> str:
        """The language of the hints sent with the audio: the one speech recognition is
        locked to if it is one of the bot's, else the player's (see Store.ui().language())."""
        locked = getattr(self.transcriber, "language", "")
        return locked if locked in LANGUAGES else self.store.ui(user_id).language()

    async def transcribe(self, user_id: int, audio: bytes, filename: str) -> str:
        lang = self.speech_language(user_id)
        names = self.context(user_id).known_names
        hint = t(lang, "stt.names", names=", ".join(names)) if names else ""
        return await self.transcriber.transcribe(audio, filename, extra_prompt=hint, lang=lang)

    async def route(self, user_id: int, transcript: str) -> RouteResult:
        ctx = self.context(user_id)
        if self.router is None:
            return rule_route(transcript, ctx)
        return await self.router.route(transcript, ctx)

    async def deliver(self, user_id: int, message: ChatMessage, voice_id: str = "") -> Delivery:
        default = self.store.get(user_id).default_channel
        message = validate(message.channel, message.target, message.text, default)
        if not (parts := split_message(message.text, self.max_message_bytes)):
            return Delivery(Ack(AckStatus.ERROR, "empty message"), message, [])
        ack = await self.hub.send(user_id, message, parts, voice_id)
        if ack.ok:
            self.store.remember(user_id, message.channel, message.target, message.text)
        log.info("user %s -> %s: %s", user_id, message.channel, ack.status)
        return Delivery(ack, message, parts)

    async def handle_agent_voice(self, user_id: int, upload: VoiceUpload) -> dict[str, Any]:
        """Push-to-talk from the agent: the same pipeline as a Telegram voice message.

        The "confirm before sending" setting is for Telegram buttons and does not apply:
        the player sees the result in the game and in the agent window.
        """
        if upload.format != "wav":
            return {"status": "bad_audio", "detail": f"format {upload.format} is not supported"}
        try:
            duration_s = wav_duration_s(upload.audio)
        except (wave.Error, EOFError) as e:
            return {"status": "bad_audio", "detail": f"not a WAV file: {e}"}
        if duration_s * 1000 < MIN_VOICE_MS:
            return {"status": "empty", "detail": "the recording is too short"}
        if duration_s > self.max_voice_s:
            return {"status": "too_long", "detail": f"the recording is longer than {self.max_voice_s:g} s"}
        try:
            transcript = await self.transcribe(user_id, upload.audio, "voice.wav")
        except TranscriptionError as e:
            log.error("STT failed for agent voice: %s", e)
            return {"status": "stt_error", "detail": "speech recognition failed"}
        if not transcript:
            return {"status": "empty", "detail": "no words heard"}
        result = await self.route(user_id, transcript)
        if result.decision is Decision.SKIP:
            return {
                "status": "skip",
                "detail": "does not look like a chat message",
                "transcript": transcript,
            } | message_fields(result.message)
        delivery = await self.deliver(user_id, result.message, upload.id)
        return {
            "status": str(delivery.ack.status),
            "detail": delivery.ack.detail,
            "transcript": transcript,
            "parts": len(delivery.parts),
        } | message_fields(delivery.message)


def wav_duration_s(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as w:
        if w.getsampwidth() == 0 or w.getframerate() <= 0:
            raise wave.Error("empty header")
        return w.getnframes() / w.getframerate()


def message_fields(message: ChatMessage) -> dict[str, str]:
    return {
        "channel": str(message.channel),
        "target": message.target,
        "label": message.label(),
        "text": message.text,
    }
