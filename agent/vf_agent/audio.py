"""Sound data helpers: WAV for the server, microphone level for the window, short beeps.

Pure Python on raw 16-bit little-endian mono PCM, so it is tested without a sound card.
"""

import io
import json
import math
import sys
import wave
from array import array

RATE = 16_000  # speech recognition does not gain anything above 16 kHz
SAMPLE_BYTES = 2


def encode_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_BYTES)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def duration_ms(pcm: bytes, rate: int = RATE) -> int:
    return len(pcm) * 1000 // (SAMPLE_BYTES * rate)


def samples(pcm: bytes) -> array:
    data = array("h")
    data.frombytes(pcm[: len(pcm) // SAMPLE_BYTES * SAMPLE_BYTES])
    if sys.byteorder == "big":
        data.byteswap()
    return data


def level(pcm: bytes) -> float:
    """Loudness for the meter: 0 = silence (-60 dBFS or less), 1 = full scale."""
    data = samples(pcm)
    if not data:
        return 0.0
    rms = math.sqrt(sum(s * s for s in data) / len(data)) / 32768
    if rms <= 0:
        return 0.0
    return max(0.0, min(1.0, 1 + 20 * math.log10(rms) / 60))


def tone(notes: list[tuple[float, float]], rate: int = 48_000, volume: float = 0.25) -> bytes:
    """Beep made of (frequency Hz, seconds) notes, with short fades so it does not click."""
    data = array("h")
    fade = int(rate * 0.005)
    for freq, seconds in notes:
        n = int(rate * seconds)
        for i in range(n):
            envelope = min(1.0, i / fade, (n - i) / fade) if fade else 1.0
            data.append(int(32767 * volume * envelope * math.sin(2 * math.pi * freq * i / rate)))
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


BEEP_RATE = 48_000
BEEPS: frozendict[str, list[tuple[float, float]]] = frozendict({
    "start": [(660, 0.07), (880, 0.09)],  # rising: listening
    "stop": [(880, 0.07), (660, 0.09)],  # falling: sent to the server
    "cancel": [(440, 0.12)],  # low: dropped, nothing sent
    "error": [(330, 0.12), (0, 0.05), (330, 0.12)],  # double low: the server could not send it
})


def voice_frame(voice_id: str, wav_bytes: bytes, ms: int) -> bytes:
    """Binary WebSocket frame for the server: JSON header line, newline, WAV file."""
    header = {"type": "voice", "id": voice_id, "format": "wav", "duration_ms": ms}
    return json.dumps(header, separators=(",", ":")).encode() + b"\n" + wav_bytes
