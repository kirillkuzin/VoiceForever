"""The real SDL3 library: a virtual gamepad and the dummy audio driver.

Runs where SDL3 can be loaded (Windows CI downloads SDL3.dll into agent/; elsewhere set
VF_SDL3 to a libSDL3 path) and is skipped otherwise.
"""

import ctypes
import threading
import time

import pytest

from vf_agent import sdl
from vf_agent.devices import Devices

try:
    LIB = sdl.SDL()
except sdl.SDLError as e:
    pytest.skip(f"SDL3 is not available: {e}", allow_module_level=True)


@pytest.fixture
def devices(monkeypatch):
    monkeypatch.setenv("SDL_AUDIO_DRIVER", "dummy")
    presses, pads, levels = [], [], []
    d = Devices(presses.append, pads.append, levels.append)
    assert d.start(), d.error
    d.presses, d.pads, d.levels = presses, pads, levels
    yield d
    d.stop()


def wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def on_sdl_thread(d, fn):
    """Run fn(sdl) on the devices thread, where SDL lives."""
    return d._submit(lambda: fn(d._sdl)).result(timeout=5)


def test_version():
    assert LIB.version.startswith("3.")


def test_virtual_gamepad_presses(devices):
    def attach(s):
        desc = sdl.VirtualJoystickDesc()
        desc.version = ctypes.sizeof(desc)
        desc.type = sdl.JOYSTICK_TYPE_GAMEPAD
        desc.nbuttons = 21
        desc.button_mask = (1 << 21) - 1
        desc.name = b"VoiceForever Test Pad"
        which = s.SDL_AttachVirtualJoystick(ctypes.byref(desc))
        assert which, s.error()
        return which, s.SDL_OpenJoystick(which)

    which, joystick = on_sdl_thread(devices, attach)
    wait(lambda: devices.pads)
    assert devices.pads[-1].name

    def press(down):
        return lambda s: s.SDL_SetJoystickVirtualButton(joystick, 8, down)  # right stick

    on_sdl_thread(devices, press(True))
    wait(lambda: devices.presses)
    on_sdl_thread(devices, press(False))
    wait(lambda: len(devices.presses) >= 2)
    assert [(p.button, p.down) for p in devices.presses[:2]] == [("rightstick", True), ("rightstick", False)]

    on_sdl_thread(devices, lambda s: (s.SDL_CloseJoystick(joystick), s.SDL_DetachVirtualJoystick(which)))
    wait(lambda: devices.pads[-1] is None)


def test_dummy_microphone_records(devices):
    assert devices.microphones().result(timeout=5) is not None
    devices.start_recording("").result(timeout=5)
    wait(lambda: devices.levels)
    time.sleep(0.3)
    pcm = devices.stop_recording().result(timeout=5)
    assert len(pcm) > 0 and len(pcm) % 2 == 0
    devices.beep("start")  # plays into the dummy output without errors
    time.sleep(0.1)
    assert devices.error is None


def test_missing_library_is_reported():
    d = Devices(lambda p: None, lambda p: None, lambda v: None, library="/nonexistent/SDL3.dll")
    assert not d.start()
    assert "SDL3" in d.error
    assert threading.active_count() >= 1
