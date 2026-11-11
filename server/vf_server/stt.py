"""Speech-to-text through any OpenAI-compatible /audio/transcriptions endpoint.

Works with OpenAI (gpt-4o-mini-transcribe, gpt-4o-transcribe, whisper-1), Groq
(whisper-large-v3-turbo) and self-hosted servers such as speaches / faster-whisper-server.

The model detects the spoken language itself unless VF_STT_LANGUAGE locks it. The prompt
lists game words players say (the stt.prompt string of the bot's locales, in the player's
language, or VF_STT_PROMPT when the server sets one) and the nicknames they use.
"""

import logging
from pathlib import PurePath

import aiohttp

from .i18n import SOURCE, t

log = logging.getLogger(__name__)

MIME_TYPES: frozendict[str, str] = frozendict({
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
})


class TranscriptionError(RuntimeError):
    pass


class Transcriber:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        language: str = "",
        prompt: str = "",
        timeout_s: float = 30.0,
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/audio/transcriptions"
        self.api_key = api_key
        self.model = model
        self.language = language.strip().lower()  # "" = the model detects it
        self.prompt = prompt  # the server's own game words, in place of stt.prompt
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def transcribe(
        self, audio: bytes, filename: str = "voice.ogg", extra_prompt: str = "", lang: str = SOURCE
    ) -> str:
        """`lang` is the language the game words of the prompt are given in."""
        form = aiohttp.FormData()
        mime = MIME_TYPES.get(PurePath(filename).suffix.lower(), "application/octet-stream")
        form.add_field("file", audio, filename=filename, content_type=mime)
        form.add_field("model", self.model)
        form.add_field("response_format", "json")
        if self.language:
            form.add_field("language", self.language)
        if prompt := f"{self.prompt or t(lang, 'stt.prompt')} {extra_prompt}".strip():
            form.add_field("prompt", prompt)

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with (
            aiohttp.ClientSession(timeout=self.timeout) as session,
            session.post(self.url, data=form, headers=headers) as resp,
        ):
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise TranscriptionError(f"STT HTTP {resp.status}: {body}")
            data = await resp.json(content_type=None)
        text = (data.get("text") or "").strip()
        log.info("transcribed %d bytes -> %d chars", len(audio), len(text))
        return text
