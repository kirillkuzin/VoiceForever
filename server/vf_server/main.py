"""Entry point: Telegram bot (long polling) + WebSocket hub for desktop agents."""

import asyncio
import logging

import anthropic
from aiogram import Bot, Dispatcher
from aiohttp import web

from .bot import build_router
from .config import Settings
from .hub import AgentHub
from .router import LLMRouter
from .service import VoiceService
from .store import Store
from .stt import Transcriber

log = logging.getLogger("voiceforever")


def voice_bytes(max_voice_s: float) -> int:
    """Frame size limit for agent recordings: WAV, mono, 16-bit, up to 48 kHz, plus headers."""
    return int(max_voice_s * 48_000 * 2) + 64 * 1024


async def run(settings: Settings) -> None:
    store = Store(settings.db_path)
    hub = AgentHub(
        settings.users,
        ack_timeout_s=settings.ack_timeout_s,
        max_voice_bytes=voice_bytes(settings.max_voice_s),
    )
    transcriber = Transcriber(
        base_url=settings.stt_base_url,
        api_key=settings.stt_api_key,
        model=settings.stt_model,
        language=settings.stt_language,
        prompt=settings.stt_prompt,
    )
    router = None
    if settings.llm_enabled:
        client = anthropic.AsyncAnthropic(timeout=settings.llm_timeout_s, max_retries=1)
        router = LLMRouter(
            client,
            model=settings.llm_model,
            effort=settings.llm_effort,
            allow_transform=settings.allow_transform,
        )
    service = VoiceService(
        store,
        hub,
        transcriber,
        router,
        settings.max_message_bytes,
        settings.known_names,
        max_voice_s=settings.max_voice_s,
    )
    hub.voice_handler = service.handle_agent_voice

    runner = web.AppRunner(hub.app())
    await runner.setup()
    await web.TCPSite(runner, settings.ws_host, settings.ws_port).start()
    log.info("agent hub listening on %s:%s", settings.ws_host, settings.ws_port)

    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    dp.include_router(build_router(service, set(settings.users)))
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        await runner.cleanup()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run(Settings.from_env()))


if __name__ == "__main__":
    main()
