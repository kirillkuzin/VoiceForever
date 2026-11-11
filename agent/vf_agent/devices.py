"""Gamepad, microphone and beeps through SDL3, on one thread of their own.

Other threads ask for things through thread-safe methods that return Futures; what happens
on the pad arrives through the callbacks, on the SDL thread.
"""

import ctypes
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from . import audio, sdl
from .ptt import BY_SDL

log = logging.getLogger("vf_agent")

POLL_S = 0.005
LEVEL_EVERY_S = 0.05


@dataclass(frozen=True, slots=True)
class Pad:
    name: str
    playstation: bool


@dataclass(frozen=True, slots=True)
class Press:
    button: str  # ptt.PadButton.name
    down: bool
    pad: Pad
    at: float  # time.monotonic()


@dataclass(slots=True)
class _Recording:
    stream: int
    device: str
    keep: bool  # False for the microphone test: only the level is needed
    pcm: bytearray
    last_level: float = 0.0


class Devices:
    def __init__(
        self,
        on_press: Callable[[Press], None],
        on_pad: Callable[[Pad | None], None],
        on_level: Callable[[float], None],
        library: str | None = None,
        gameinput: bool = False,
    ) -> None:
        self.on_press, self.on_pad, self.on_level = on_press, on_pad, on_level
        self.library = library
        self.gameinput = gameinput  # SDL reads the Xbox Share button only through GameInput
        self.error: str | None = None  # why the pad and microphone do not work, for the window
        self.version = ""
        self.pad: Pad | None = None
        self._jobs: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...], Future[Any]]] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vf-devices", daemon=True)
        self._sdl: sdl.SDL | None = None
        self._pads: dict[int, tuple[int, Pad]] = {}  # instance id -> (SDL_Gamepad*, Pad)
        self._rec: _Recording | None = None
        self._playback: int | None = None

    # ------------------------------------------------------------ any thread
    def start(self, timeout: float = 10.0) -> bool:
        """Start SDL. False when it is missing or fails: `error` says why; PTT stays off."""
        self._thread.start()
        self._ready.wait(timeout)
        return self.error is None

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def microphones(self) -> Future[list[str]]:
        return self._submit(self._microphones)

    def start_recording(self, device: str, keep: bool = True) -> Future[str]:
        """Open the microphone ("" = the Windows default). Returns the device actually used."""
        return self._submit(self._start_recording, device, keep)

    def stop_recording(self) -> Future[bytes]:
        """Close the microphone and return what it heard: 16 kHz mono 16-bit PCM."""
        return self._submit(self._stop_recording)

    def beep(self, name: str) -> None:
        self._submit(self._beep, name)

    def _submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        if self.error is not None or not self._thread.is_alive():
            future.set_exception(RuntimeError(self.error or "геймпад и микрофон не запущены"))
        else:
            self._jobs.put((fn, args, future))
        return future

    # ------------------------------------------------------------ SDL thread
    def _run(self) -> None:
        try:
            self._sdl = sdl.SDL(self.library)
            if self.gameinput:
                self._sdl.SDL_SetHint(b"SDL_JOYSTICK_GAMEINPUT", b"1")
            self._sdl.init(sdl.INIT_GAMEPAD | sdl.INIT_AUDIO)
            self.version = self._sdl.version
            log.info("SDL %s, audio driver %s", self.version, self._sdl.text(self._sdl.SDL_GetCurrentAudioDriver()))
        except (sdl.SDLError, OSError, AttributeError) as e:
            self.error = str(e)
            log.error("gamepad and microphone are off: %s", e)
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                self._do_jobs()
                self._poll_events()
                self._pump_audio()
                time.sleep(POLL_S)
        except Exception:
            log.exception("gamepad and microphone thread failed")
            self.error = "геймпад и микрофон остановились, подробности в журнале"
        finally:
            self._shutdown()

    def _do_jobs(self) -> None:
        while True:
            try:
                fn, args, future = self._jobs.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args))
                except Exception as e:
                    future.set_exception(e)

    def _poll_events(self) -> None:
        s = self._sdl
        event = sdl.Event()
        while s.SDL_PollEvent(ctypes.byref(event)):
            match event.type:
                case sdl.EVENT_GAMEPAD_ADDED:
                    self._open_pad(event.device.which)
                case sdl.EVENT_GAMEPAD_REMOVED:
                    self._close_pad(event.device.which)
                case sdl.EVENT_GAMEPAD_BUTTON_DOWN | sdl.EVENT_GAMEPAD_BUTTON_UP:
                    e = event.gbutton
                    button = BY_SDL.get(e.button)
                    entry = self._pads.get(e.which)
                    if button is None or entry is None:
                        continue
                    pad = entry[1]
                    if pad != self.pad:  # the pad in use is the one pressed last
                        self.pad = pad
                        self.on_pad(pad)
                    self.on_press(Press(button.name, bool(e.down), pad, time.monotonic()))

    def _open_pad(self, which: int) -> None:
        s = self._sdl
        if which in self._pads:
            return
        handle = s.SDL_OpenGamepad(which)
        if not handle:
            log.warning("cannot open gamepad %s: %s", which, s.error())
            return
        pad = Pad(s.text(s.SDL_GetGamepadName(handle)) or "Геймпад", s.SDL_GetGamepadType(handle) in sdl.PLAYSTATION_TYPES)
        self._pads[which] = (handle, pad)
        log.info("gamepad connected: %s", pad.name)
        self.pad = pad
        self.on_pad(pad)

    def _close_pad(self, which: int) -> None:
        entry = self._pads.pop(which, None)
        if entry is None:
            return
        self._sdl.SDL_CloseGamepad(entry[0])
        log.info("gamepad disconnected: %s", entry[1].name)
        if self.pad == entry[1]:
            self.pad = next((p for _, p in self._pads.values()), None)
            self.on_pad(self.pad)

    def _microphones(self) -> list[str]:
        s = self._sdl
        names = [s.text(s.SDL_GetAudioDeviceName(i)) for i in s.ids("SDL_GetAudioRecordingDevices")]
        return list(dict.fromkeys(n for n in names if n))

    def _device_id(self, name: str) -> tuple[int, str]:
        s = self._sdl
        if name:
            for devid in s.ids("SDL_GetAudioRecordingDevices"):
                if s.text(s.SDL_GetAudioDeviceName(devid)) == name:
                    return devid, name
            log.warning("microphone %r not found, using the Windows default", name)
        return sdl.DEFAULT_RECORDING, ""

    def _start_recording(self, device: str, keep: bool) -> str:
        if self._rec is not None:
            self._close_recording()
        s = self._sdl
        devid, used = self._device_id(device)
        spec = sdl.AudioSpec(sdl.AUDIO_S16LE, 1, audio.RATE)
        stream = s.SDL_OpenAudioDeviceStream(devid, ctypes.byref(spec), None, None)
        if not stream:
            raise RuntimeError(f"микрофон не открылся: {s.error()}")
        if not s.SDL_ResumeAudioStreamDevice(stream):
            s.SDL_DestroyAudioStream(stream)
            raise RuntimeError(f"микрофон не запустился: {s.error()}")
        self._rec = _Recording(stream, used, keep, bytearray())
        return used

    def _pump_audio(self) -> None:
        rec = self._rec
        if rec is None:
            return
        s = self._sdl
        available = s.SDL_GetAudioStreamAvailable(rec.stream)
        if available <= 0:
            return
        buf = ctypes.create_string_buffer(available)
        got = s.SDL_GetAudioStreamData(rec.stream, buf, available)
        if got <= 0:
            return
        chunk = buf.raw[:got]
        if rec.keep:
            rec.pcm += chunk
        now = time.monotonic()
        if now - rec.last_level >= LEVEL_EVERY_S:
            rec.last_level = now
            self.on_level(audio.level(chunk))

    def _stop_recording(self) -> bytes:
        if self._rec is None:
            return b""
        self._pump_audio()
        pcm = bytes(self._rec.pcm)
        self._close_recording()
        return pcm

    def _close_recording(self) -> None:
        rec, self._rec = self._rec, None
        if rec is not None:
            self._sdl.SDL_DestroyAudioStream(rec.stream)
            self.on_level(0.0)

    def _beep(self, name: str) -> None:
        s = self._sdl
        if self._playback is None:
            spec = sdl.AudioSpec(sdl.AUDIO_S16LE, 1, audio.BEEP_RATE)
            stream = s.SDL_OpenAudioDeviceStream(sdl.DEFAULT_PLAYBACK, ctypes.byref(spec), None, None)
            if not stream:
                log.warning("no sound output for beeps: %s", s.error())
                return
            s.SDL_ResumeAudioStreamDevice(stream)
            self._playback = stream
        data = _BEEP_DATA.get(name)
        if data:
            s.SDL_PutAudioStreamData(self._playback, data, len(data))

    def _shutdown(self) -> None:
        s = self._sdl
        if s is None:
            return
        self._close_recording()
        if self._playback is not None:
            s.SDL_DestroyAudioStream(self._playback)
            self._playback = None
        for handle, _ in self._pads.values():
            s.SDL_CloseGamepad(handle)
        self._pads.clear()
        s.SDL_QuitSubSystem(sdl.INIT_GAMEPAD | sdl.INIT_AUDIO)
        while True:  # fail what is still queued, so no caller waits forever
            try:
                _, _, future = self._jobs.get_nowait()
            except queue.Empty:
                break
            future.cancel()


_BEEP_DATA: frozendict[str, bytes] = frozendict(
    {name: audio.tone(notes, audio.BEEP_RATE) for name, notes in audio.BEEPS.items()}
)
