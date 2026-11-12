"""Entry point: WebSocket hub for desktop agents + Telegram bot (long polling), if configured."""

import asyncio
import contextlib
import logging
import signal

import anthropic
from aiogram import Bot, Dispatcher
from aiohttp import web

from .access import Access, LocalAccess, RemoteAccess
from .bot import setup_bot
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


def make_access(settings: Settings) -> Access:
    if settings.access_url:
        log.info("players and limits come from %s", settings.access_url)
        return RemoteAccess(settings.access_url, settings.access_secret, settings.access_timeout_s)
    return LocalAccess(settings.users)


async def run(settings: Settings) -> None:
    store = Store(settings.db_path)
    access = make_access(settings)
    hub = AgentHub(
        access,
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
        rate_per_min=settings.rate_per_min,
    )
    hub.voice_handler = service.handle_agent_voice

    runner = web.AppRunner(hub.app())
    await runner.setup()
    await web.TCPSite(runner, settings.ws_host, settings.ws_port).start()
    log.info("agent hub listening on %s:%s", settings.ws_host, settings.ws_port)

    try:
        if settings.telegram_token:
            await run_bot(settings, service)
        else:
            log.info("no VF_TELEGRAM_TOKEN: agents only, without the Telegram bot")
            await until_stopped()
    finally:
        await runner.cleanup()
        await service.drain()
        await access.close()


async def until_stopped() -> None:
    """Wait for Ctrl+C or SIGTERM (docker stop)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run_bot(settings: Settings, service: VoiceService) -> None:
    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    allowed_updates = setup_bot(
        dp,
        service,
        settings.agent_download_url,
        terms_url=settings.terms_url,
        pay_support=settings.pay_support,
    )
    try:
        await dp.start_polling(bot, handle_signals=True, allowed_updates=allowed_updates)
    finally:
        await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run(Settings.from_env()))


if __name__ == "__main__":
    main()
