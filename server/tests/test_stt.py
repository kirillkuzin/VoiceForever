"""Speech recognition: language detection by default, hints in the player's language."""

from aiohttp import web
from aiohttp.test_utils import TestServer

from vf_server.access import LocalAccess
from vf_server.config import Settings
from vf_server.i18n import t
from vf_server.service import VoiceService
from vf_server.store import Store
from vf_server.stt import Transcriber

USER = 42


async def transcribe_on_fake_server(transcriber_kwargs, **call):
    """Runs one transcription against a local /audio/transcriptions; returns the form fields."""
    seen: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        async for part in await request.multipart():
            seen[part.name] = (await part.read()).decode() if part.name != "file" else part.filename
        return web.json_response({"text": " Привет "})

    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", handler)
    async with TestServer(app) as server:
        stt = Transcriber(str(server.make_url("/v1")), "", "whisper", **transcriber_kwargs)
        assert await stt.transcribe(b"ogg", "voice.ogg", **call) == "Привет"
    return seen


async def test_the_model_detects_the_language_unless_the_server_locks_it():
    seen = await transcribe_on_fake_server({}, extra_prompt="Names: Arthorn.")
    assert "language" not in seen
    assert seen["prompt"] == t("en", "stt.prompt") + " Names: Arthorn."
    assert seen["file"] == "voice.ogg" and seen["model"] == "whisper"

    seen = await transcribe_on_fake_server({"language": " RU "}, lang="ru")
    assert seen["language"] == "ru" and seen["prompt"] == t("ru", "stt.prompt")

    seen = await transcribe_on_fake_server({"prompt": "Ragnaros, Onyxia"}, lang="de")
    assert seen["prompt"] == "Ragnaros, Onyxia"


async def test_the_players_language_wins_over_the_servers():
    seen = await transcribe_on_fake_server({"language": "ru"}, language="de", lang="de")
    assert seen["language"] == "de" and seen["prompt"] == t("de", "stt.prompt")
    seen = await transcribe_on_fake_server({}, language=" FR ")
    assert seen["language"] == "fr"
    seen = await transcribe_on_fake_server({"language": "ru"}, language="")  # not set: the server's
    assert seen["language"] == "ru"


def test_language_setting_defaults_to_detection():
    base = {"VF_TELEGRAM_TOKEN": "x", "VF_USERS": "42:agent-token-0123456789"}
    assert Settings.from_env(base).stt_language == ""
    assert Settings.from_env(base | {"VF_STT_LANGUAGE": "ru"}).stt_language == "ru"


class RecordingTranscriber:
    def __init__(self, language=""):
        self.language = language
        self.calls = []

    async def transcribe(self, audio, filename, extra_prompt="", lang="en", language=""):
        self.calls.append((lang, extra_prompt) + ((language,) if language else ()))
        return "hallo"


async def test_hints_come_in_the_player_language():
    store = Store(":memory:")
    store.update(USER, known_names="Arthorn, Liana")
    stt = RecordingTranscriber()
    service = VoiceService(store, hub=None, transcriber=stt, router=None, access=LocalAccess({}))

    await service.transcribe(USER, b"ogg", "voice.ogg")
    store.update_ui(USER, tg_lang="de-DE")
    await service.transcribe(USER, b"ogg", "voice.ogg")
    store.update_ui(USER, lang="fr")
    await service.transcribe(USER, b"ogg", "voice.ogg")
    assert stt.calls == [
        ("en", "Names: Arthorn, Liana."),
        ("de", "Namen: Arthorn, Liana."),
        ("fr", "Noms : Arthorn, Liana."),
    ]

    stt.language = "ru"  # the server locks recognition to Russian: hints follow it
    assert service.speech_language(USER) == "ru"
    stt.language = "ja"  # not a bot language: the player's stays
    assert service.speech_language(USER) == "fr"

    # The language set on the player's account wins over the server's, and the hints follow it
    stt.language = "ru"
    await service.transcribe(USER, b"ogg", "voice.ogg", language="de")
    assert stt.calls[-1] == ("de", "Namen: Arthorn, Liana.", "de")
    assert service.speech_language(USER, "es") == "es"
    assert service.speech_language(USER, "uk") == "fr"  # not a bot language: the player's
