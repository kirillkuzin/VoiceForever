"""Small Win32 helpers for the window app: one running copy, message boxes, autostart, console."""

import contextlib
import ctypes
import sys
import threading
import winreg
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("winapi works only on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
ASFW_ANY = 0xFFFFFFFF
ATTACH_PARENT_PROCESS = 0xFFFFFFFF
MB_OK = 0x0
MB_ICONERROR = 0x10

kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.AttachConsole.argtypes = (wintypes.DWORD,)
kernel32.AttachConsole.restype = wintypes.BOOL
user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)


class SingleInstance:
    """A named mutex marks the running agent; a named event asks it to show its window."""

    def __init__(self, name: str = "VoiceForeverAgent") -> None:
        self._mutex_name = f"Local\\{name}"
        self._event_name = f"Local\\{name}.Show"
        self._mutex: int | None = None
        self._event: int | None = None
        self._stopping = False

    def acquire(self) -> bool:
        """True for the first copy. A second copy should call show_running() and exit."""
        self._mutex = kernel32.CreateMutexW(None, False, self._mutex_name)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        self._event = kernel32.CreateEventW(None, False, False, self._event_name)
        return True

    def show_running(self) -> None:
        event = kernel32.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False, self._event_name)
        if event:
            user32.AllowSetForegroundWindow(ASFW_ANY)  # let the running copy come to the front
            kernel32.SetEvent(event)
            kernel32.CloseHandle(event)

    def listen(self, on_show: Callable[[], None]) -> None:
        def wait() -> None:
            while kernel32.WaitForSingleObject(self._event, INFINITE) == WAIT_OBJECT_0:
                if self._stopping:
                    return
                on_show()

        if self._event:
            threading.Thread(target=wait, name="vf-show-requests", daemon=True).start()

    def release(self) -> None:
        self._stopping = True
        if self._event:
            kernel32.SetEvent(self._event)


def message_box(text: str, title: str = "VoiceForever") -> None:
    user32.MessageBoxW(None, text, title, MB_OK | MB_ICONERROR)


def attach_parent_console() -> None:
    """The exe has no console of its own. When started from cmd or PowerShell, print there."""
    if sys.stdout is None and kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "VoiceForever Agent"


def autostart_command(config_path: Path) -> str | None:
    """Command line for Windows to start the agent at sign-in; only the built exe has one."""
    if not getattr(sys, "frozen", False):
        return None
    return f'"{sys.executable}" --minimized --config "{config_path}"'


def set_autostart(enabled: bool, config_path: Path) -> None:
    command = autostart_command(config_path)
    if command is None:  # running from source: leave the exe's entry alone
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, RUN_VALUE)
