"""Windows input backend: foreground window check, SendInput keystrokes, clipboard paste.

Only standard user-level Win32 calls are used (the same ones as on-screen keyboards and
voice typing). Nothing reads or writes game memory.
"""

import ctypes
import os
import sys
import time
from collections.abc import Iterable
from ctypes import wintypes

from .plan import KEY_CODES, Action, GameState, Kind, parse_combo

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("winio works only on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = (wintypes.HANDLE,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HANDLE,)
kernel32.GlobalFree.argtypes = (wintypes.HANDLE,)
kernel32.GlobalFree.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

EXTENDED = frozenset({0x24, 0x23, 0x2D, 0x2E, 0x21, 0x22})


def _key_input(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags))


def press(combo: str) -> None:
    """Press a key combination atomically (one SendInput call)."""
    vks = [KEY_CODES[k] for k in parse_combo(combo)]
    events = [_key_input(vk, False) for vk in vks] + [_key_input(vk, True) for vk in reversed(vks)]
    arr = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(ctypes.get_last_error(), "SendInput was blocked")


# ------------------------------------------------------------------ clipboard
def _open_clipboard() -> None:
    for _ in range(20):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.01)
    raise OSError(ctypes.get_last_error(), "clipboard is busy")


def get_clipboard_text() -> str | None:
    _open_clipboard()
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError(ctypes.get_last_error(), "SetClipboardData failed")
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ window
def _title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _exe_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        return ""
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(proc, 0, path, ctypes.byref(size)):
            return os.path.basename(path.value)
        return ""
    finally:
        kernel32.CloseHandle(proc)


def foreground_window() -> tuple[str, str]:
    """(window title, process exe name) of the active window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    return _title(hwnd), _exe_name(hwnd)


def visible_windows() -> list[int]:
    """Handles of visible top-level windows that have a title."""
    found: list[int] = []

    @WNDENUMPROC
    def collect(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(collect, 0)
    return found


class WindowsBackend:
    def __init__(
        self, window_titles: Iterable[str], process_names: Iterable[str], key_delay_ms: int = 15
    ) -> None:
        self.window_titles = [t.lower() for t in window_titles]
        self.process_names = [p.lower() for p in process_names]
        self.key_delay = key_delay_ms / 1000

    def _is_game(self, title: str, exe: str) -> bool:
        if exe and exe.lower() in self.process_names:
            return True
        return any(title.lower().startswith(t) for t in self.window_titles if t)

    def is_game_focused(self) -> bool:
        return self._is_game(*foreground_window())

    def game_state(self) -> GameState:
        foreground = user32.GetForegroundWindow()
        if foreground and self._is_game(_title(foreground), _exe_name(foreground)):
            return GameState.ACTIVE
        for hwnd in visible_windows():
            if hwnd != foreground and self._is_game(_title(hwnd), _exe_name(hwnd)):
                return GameState.BACKGROUND
        return GameState.CLOSED

    def run(self, actions: list[Action]) -> None:
        saved = get_clipboard_text()
        pasted = False
        try:
            for action in actions:
                match action:
                    case Action(Kind.SLEEP, int(ms)):
                        time.sleep(ms / 1000)
                    case Action(Kind.KEY, str(combo)):
                        press(combo)
                        time.sleep(self.key_delay)
                    case Action(Kind.PASTE, str(text)):
                        set_clipboard_text(text)
                        pasted = True
                        time.sleep(0.02)
                        press("ctrl+v")
                        time.sleep(0.05)  # let the game read the clipboard before the next key
                    case _:
                        raise ValueError(f"unknown action {action!r}")
        finally:
            if pasted and saved is not None:
                time.sleep(0.15)
                try:
                    set_clipboard_text(saved)
                except OSError:
                    pass
