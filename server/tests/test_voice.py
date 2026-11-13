"""Push-to-talk from the agent: a WAV in a binary frame goes through STT, routing and typing."""

import asyncio
import contextlib
import io
import json
import wave

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from vf_server.hub import AgentHub, VoiceUpload, parse_voice_frame
from vf_server.main import voice_bytes
from vf_server.service import VoiceService, wav_duration_s
from vf_server.store import Store
from vf_server.stt import TranscriptionError

TOKEN = "test-token-0123456789abcdef"
USER = 42


def wav(seconds: float, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(seconds * rate))
    return buf.getvalue()


def frame(voice_id: str, audio: bytes, fmt: str = "wav") -> bytes:
    header = {"type": "voice", "id": voice_id, "format": fmt, "duration_ms": 1000}
    return json.dumps(header).encode() + b"\n" + audio


class FakeTranscriber:
    def __init__(self, text="", error=False):
        self.text, self.error, self.files, self.languages = text, error, [], []

    async def transcribe(self, audio, filename, extra_prompt="", lang="en", language=""):
        self.files.append((filename, len(audio)))
        self.languages.append((language, lang))
        if self.error:
            raise TranscriptionError("STT HTTP 500")
        return self.text


async def agent(url, uploads, results, typed, ack_status="ok", expect=None):
    """A fake desktop agent: sends recordings, acks typing, collects voice results."""
    expect = len(uploads) if expect is None else expect
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send_json({"type": "hello", "version": "0.3.0", "mode": "addon"})
            for data in uploads:
                await ws.send_bytes(data)
            async for msg in ws:
                data = json.loads(msg.data)
                match data["type"]:
                    case "send":
                        typed.append(data)
                        await ws.send_json({"type": "ack", "id": data["id"], "status": ack_status})
                    case "voice_result":
                        results.append(data)
                        if len(results) == expect:
                            return


async def run(service_or_none, uploads, transcriber=None, ack_status="ok", expect=None, **hub_kwargs):
    hub = AgentHub({USER: TOKEN}, ack_timeout_s=2, **hub_kwargs)
    if service_or_none is not False:
        service = VoiceService(Store(":memory:"), hub, transcriber or FakeTranscriber(), router=None)
        hub.voice_handler = service.handle_agent_voice
    results, typed = [], []
    async with TestServer(hub.app()) as server:
        async with asyncio.timeout(5):
            await agent(server.make_url("/ws"), uploads, results, typed, ack_status, expect)
    return results, typed


def test_parse_voice_frame():
    upload = parse_voice_frame(frame("abc", b"RIFF...."))
    assert upload == VoiceUpload("abc", "wav", b"RIFF....", 1000)
    # The audio may contain newlines: only the first one ends the header.
    assert parse_voice_frame(frame("x", b"a\nb")).audio == b"a\nb"


@pytest.mark.parametrize(
    "data",
    [
        b"no header",
        b"not json\nRIFF",
        b'{"type": "send", "id": "1", "format": "wav"}\nRIFF',
        b'{"type": "voice", "format": "wav"}\nRIFF',
        b'{"type": "voice", "id": "", "format": "wav"}\nRIFF',
        b"x" * 2000 + b"\nRIFF",
    ],
)
def test_parse_voice_frame_rejects(data):
    with pytest.raises(ValueError):
        parse_voice_frame(data)


def test_wav_duration():
    assert wav_duration_s(wav(1.5)) == pytest.approx(1.5)


def test_frame_limit_fits_max_recording():
    # 16 kHz mono 16-bit is what the agent sends; 48 kHz still fits.
    assert len(wav(60, rate=48_000)) < voice_bytes(60)


async def test_voice_is_typed_and_reported():
    stt = FakeTranscriber("в гильдию всем привет")
    results, typed = await run(None, [frame("v1", wav(1.2))], stt)
    assert stt.files == [("voice.wav", len(wav(1.2)))]
    assert typed[0]["channel"] == "guild" and typed[0]["parts"] == ["Всем привет"]
    assert typed[0]["voice_id"] == "v1"
    [result] = results
    assert result["type"] == "voice_result" and result["id"] == "v1"
    assert result["status"] == "ok"
    assert (result["channel"], result["label"], result["text"]) == ("guild", "Guild", "Всем привет")
    assert result["transcript"] == "в гильдию всем привет"


async def test_not_focused_reaches_the_result():
    stt = FakeTranscriber("привет")
    results, _ = await run(None, [frame("v1", wav(1))], stt, ack_status="not_focused")
    assert results[0]["status"] == "not_focused"


@pytest.mark.parametrize(
    "upload, stt, status",
    [
        (frame("v", wav(0.1)), FakeTranscriber("привет"), "empty"),
        (frame("v", wav(1)), FakeTranscriber(""), "empty"),
        (frame("v", wav(1)), FakeTranscriber(error=True), "stt_error"),
        (frame("v", b"not a wav file at all"), FakeTranscriber("привет"), "bad_audio"),
        (frame("v", wav(1), fmt="ogg"), FakeTranscriber("привет"), "bad_audio"),
        (frame("v", wav(61)), FakeTranscriber("привет"), "too_long"),
    ],
)
async def test_refused_voice(upload, stt, status):
    results, typed = await run(None, [upload], stt)
    assert results[0]["status"] == status
    assert results[0]["detail"]
    assert typed == []


async def test_oversized_recording_is_refused_without_stt():
    stt = FakeTranscriber("привет")
    results, typed = await run(None, [frame("v", wav(0.04))], stt, max_voice_bytes=1000)
    assert results[0]["status"] == "too_long"
    assert stt.files == [] and typed == []


async def test_server_without_voice_handler():
    results, _ = await run(False, [frame("v", wav(1))])
    assert results[0]["status"] == "unsupported"


async def test_recordings_are_handled_in_order():
    stt = FakeTranscriber("привет")
    results, typed = await run(None, [frame("a", wav(1)), frame("b", wav(1))], stt)
    assert [r["id"] for r in results] == ["a", "b"]
    assert len(typed) == 2


async def test_bad_frame_is_ignored_and_connection_stays():
    stt = FakeTranscriber("привет")
    results, _ = await run(None, [b"garbage", frame("ok", wav(1))], stt, expect=1)
    assert [r["id"] for r in results] == ["ok"]
