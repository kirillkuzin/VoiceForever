"""The few SDL3 calls the agent needs, through ctypes: gamepad buttons and audio.

SDL3.dll ships inside the exe. SDL reads Xbox, PlayStation and Switch pads, also while the
game window is in front (the background events hint below), and records from the
microphones Windows lists, with their full names.

All calls go through one thread (devices.Devices), as SDL expects for events.
"""

import ctypes
import ctypes.util
import os
import sys
from collections.abc import Iterator
from ctypes import POINTER, c_bool, c_char_p, c_int, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p
from pathlib import Path

INIT_AUDIO = 0x00000010
INIT_GAMEPAD = 0x00002000

EVENT_GAMEPAD_BUTTON_DOWN = 0x651
EVENT_GAMEPAD_BUTTON_UP = 0x652
EVENT_GAMEPAD_ADDED = 0x653
EVENT_GAMEPAD_REMOVED = 0x654
EVENT_AUDIO_DEVICE_ADDED = 0x1100
EVENT_AUDIO_DEVICE_REMOVED = 0x1101

AUDIO_S16LE = 0x8010
DEFAULT_PLAYBACK = 0xFFFFFFFF
DEFAULT_RECORDING = 0xFFFFFFFE

JOYSTICK_TYPE_GAMEPAD = 1

# Gamepad types (SDL_GamepadType) that use PlayStation button names.
PLAYSTATION_TYPES = frozenset({4, 5, 6})  # PS3, PS4, PS5

HINTS: frozendict[str, str] = frozendict({
    # The game is in front while the player talks: read the pad anyway.
    "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS": "1",
    # Do not switch PlayStation pads into SDL's extended report mode: the game reads them too.
    "SDL_JOYSTICK_ENHANCED_REPORTS": "auto",
    "SDL_JOYSTICK_HIDAPI_PS5_PLAYER_LED": "0",
    # The name in the Windows volume mixer and the microphone privacy list.
    "SDL_APP_NAME": "VoiceForever",
    "SDL_AUDIO_DEVICE_STREAM_NAME": "VoiceForever",
})


class SDLError(RuntimeError):
    pass


class GamepadButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", c_uint32),
        ("reserved", c_uint32),
        ("timestamp", c_uint64),
        ("which", c_uint32),
        ("button", c_uint8),
        ("down", c_bool),
        ("padding1", c_uint8),
        ("padding2", c_uint8),
    ]


class DeviceEvent(ctypes.Structure):
    """SDL_GamepadDeviceEvent and SDL_AudioDeviceEvent start the same way."""

    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64), ("which", c_uint32)]


class Event(ctypes.Union):
    _fields_ = [
        ("type", c_uint32),
        ("gbutton", GamepadButtonEvent),
        ("device", DeviceEvent),
        ("padding", c_uint8 * 128),
        ("_align", c_uint64),
    ]


class AudioSpec(ctypes.Structure):
    _fields_ = [("format", c_int), ("channels", c_int), ("freq", c_int)]


class VirtualJoystickDesc(ctypes.Structure):
    """For tests: a virtual pad whose buttons the test presses (SDL_AttachVirtualJoystick)."""

    _fields_ = [
        ("version", c_uint32),
        ("type", c_uint16),
        ("padding", c_uint16),
        ("vendor_id", c_uint16),
        ("product_id", c_uint16),
        ("naxes", c_uint16),
        ("nbuttons", c_uint16),
        ("nballs", c_uint16),
        ("nhats", c_uint16),
        ("ntouchpads", c_uint16),
        ("nsensors", c_uint16),
        ("padding2", c_uint16 * 2),
        ("button_mask", c_uint32),
        ("axis_mask", c_uint32),
        ("name", c_char_p),
        ("touchpads", c_void_p),
        ("sensors", c_void_p),
        ("userdata", c_void_p),
        ("callbacks", c_void_p * 8),  # Update ... Cleanup, all NULL
    ]


# name: (result type, argument types)
SIGNATURES: frozendict[str, tuple[object, tuple[object, ...]]] = frozendict({
    "SDL_GetVersion": (c_int, ()),
    "SDL_GetError": (c_char_p, ()),
    "SDL_SetHint": (c_bool, (c_char_p, c_char_p)),
    "SDL_InitSubSystem": (c_bool, (c_uint32,)),
    "SDL_QuitSubSystem": (None, (c_uint32,)),
    "SDL_Quit": (None, ()),
    "SDL_free": (None, (c_void_p,)),
    "SDL_PollEvent": (c_bool, (POINTER(Event),)),
    "SDL_GetGamepads": (POINTER(c_uint32), (POINTER(c_int),)),
    "SDL_OpenGamepad": (c_void_p, (c_uint32,)),
    "SDL_CloseGamepad": (None, (c_void_p,)),
    "SDL_GetGamepadName": (c_char_p, (c_void_p,)),
    "SDL_GetGamepadType": (c_int, (c_void_p,)),
    "SDL_GetCurrentAudioDriver": (c_char_p, ()),
    "SDL_GetAudioRecordingDevices": (POINTER(c_uint32), (POINTER(c_int),)),
    "SDL_GetAudioDeviceName": (c_char_p, (c_uint32,)),
    "SDL_OpenAudioDeviceStream": (c_void_p, (c_uint32, POINTER(AudioSpec), c_void_p, c_void_p)),
    "SDL_ResumeAudioStreamDevice": (c_bool, (c_void_p,)),
    "SDL_GetAudioStreamAvailable": (c_int, (c_void_p,)),
    "SDL_GetAudioStreamData": (c_int, (c_void_p, c_void_p, c_int)),
    "SDL_GetAudioStreamQueued": (c_int, (c_void_p,)),
    "SDL_PutAudioStreamData": (c_bool, (c_void_p, c_void_p, c_int)),
    "SDL_DestroyAudioStream": (None, (c_void_p,)),
    "SDL_AttachVirtualJoystick": (c_uint32, (POINTER(VirtualJoystickDesc),)),
    "SDL_DetachVirtualJoystick": (c_bool, (c_uint32,)),
    "SDL_OpenJoystick": (c_void_p, (c_uint32,)),
    "SDL_CloseJoystick": (None, (c_void_p,)),
    "SDL_SetJoystickVirtualButton": (c_bool, (c_void_p, c_int, c_bool)),
})


def library_name() -> str:
    match sys.platform:
        case "win32":
            return "SDL3.dll"
        case "darwin":
            return "libSDL3.dylib"
        case _:
            return "libSDL3.so.0"


def candidates() -> Iterator[str]:
    """Where SDL3 may be: VF_SDL3 for development, the exe bundle, next to the exe, the system."""
    name = library_name()
    if env := os.environ.get("VF_SDL3"):
        yield env
    if bundle := getattr(sys, "_MEIPASS", None):
        yield str(Path(bundle) / name)
    if getattr(sys, "frozen", False):
        yield str(Path(sys.executable).resolve().parent / name)
    yield str(Path(__file__).resolve().parents[1] / name)  # agent/SDL3.dll in a source checkout
    if found := ctypes.util.find_library("SDL3"):
        yield found


def version_text(number: int) -> str:
    return f"{number // 1_000_000}.{number // 1000 % 1000}.{number % 1000}"


class SDL:
    """Loaded SDL3 library with typed functions as attributes: sdl.SDL_PollEvent(...)."""

    def __init__(self, path: str | None = None) -> None:
        errors = []
        for candidate in [path] if path else candidates():
            if path is None and os.sep in candidate and not Path(candidate).exists():
                continue
            try:
                self.lib = ctypes.CDLL(candidate)
                break
            except OSError as e:
                errors.append(f"{candidate}: {e}")
        else:
            raise SDLError("SDL3 не найден: " + ("; ".join(errors) or library_name()))
        for name, (restype, argtypes) in SIGNATURES.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = restype, argtypes
            setattr(self, name, fn)
        self.version = version_text(self.SDL_GetVersion())

    def error(self) -> str:
        return (self.SDL_GetError() or b"").decode("utf-8", "replace")

    def init(self, flags: int) -> None:
        for key, value in HINTS.items():
            self.SDL_SetHint(key.encode(), value.encode())
        if not self.SDL_InitSubSystem(flags):
            raise SDLError(f"SDL_InitSubSystem: {self.error()}")

    def ids(self, getter: str) -> list[int]:
        """Call an SDL_Get...s(int *count) function and free the array it returns."""
        count = c_int(0)
        array = getattr(self, getter)(ctypes.byref(count))
        if not array:
            return []
        try:
            return [array[i] for i in range(count.value)]
        finally:
            self.SDL_free(ctypes.cast(array, c_void_p))

    def text(self, value: bytes | None) -> str:
        return (value or b"").decode("utf-8", "replace")
