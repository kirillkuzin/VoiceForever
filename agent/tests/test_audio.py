"""WAV, level meter, beeps and the voice frame (checked against the server's parser)."""

import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from vf_server.hub import parse_voice_frame  # noqa: E402

from vf_agent import audio  # noqa: E402


def wav_duration_s(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def test_wav_round_trip():
    pcm = audio.tone([(440, 0.5)], rate=audio.RATE)
    data = audio.encode_wav(pcm)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16_000)
        assert w.readframes(w.getnframes()) == pcm
    assert audio.duration_ms(pcm) == 500
    assert wav_duration_s(data) == 0.5


def test_level():
    assert audio.level(b"") == 0.0
    assert audio.level(b"\0\0" * 100) == 0.0
    loud = audio.level(audio.tone([(440, 0.1)], volume=0.9))
    quiet = audio.level(audio.tone([(440, 0.1)], volume=0.01))
    assert 0.9 < loud <= 1.0 and 0.0 < quiet < loud


def test_beeps_are_short():
    for notes in audio.BEEPS.values():
        assert 0 < sum(seconds for _, seconds in notes) < 0.5


def test_voice_frame_is_what_the_server_reads():
    pcm = audio.tone([(300, 1.0)], rate=audio.RATE)
    frame = audio.voice_frame("abc123", audio.encode_wav(pcm), audio.duration_ms(pcm))
    upload = parse_voice_frame(frame)
    assert (upload.id, upload.format, upload.duration_ms) == ("abc123", "wav", 1000)
    assert wav_duration_s(upload.audio) == 1.0
