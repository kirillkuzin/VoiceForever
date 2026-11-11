"""Notification area (tray) icon on plain Win32 calls, with its own thread and message loop.

The menu and the pop-up notifications are drawn by Windows; only the icon is ours.
"""

import ctypes
import logging
import queue
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

from .icons import DOTS, icon_image

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("tray works only on Windows")

log = logging.getLogger("vf_agent")

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # mouse events on the icon
WM_WAKE = WM_APP + 2  # run queued work on the tray thread
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING = 0x01, 0x02
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
SM_CXSMICON = 49


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)  # fmt: skip
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = (wintypes.HWND,)
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = LRESULT
user32.PostQuitMessage.argtypes = (ctypes.c_int,)
user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
user32.SetMenuDefaultItem.argtypes = (wintypes.HMENU, wintypes.UINT, wintypes.UINT)
user32.TrackPopupMenu.argtypes = (
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
)  # fmt: skip
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = (wintypes.HMENU,)
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.CreateIconFromResourceEx.argtypes = (
    ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT,
)  # fmt: skip
user32.CreateIconFromResourceEx.restype = wintypes.HICON
user32.DestroyIcon.argtypes = (wintypes.HICON,)
shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

CLASS_NAME = "VoiceForeverTray"

type MenuItem = tuple[str, Callable[[], None]] | None  # None draws a separator


def make_icon(size: int, dot: str) -> int:
    data = icon_image(size, DOTS[dot])
    return user32.CreateIconFromResourceEx(data, len(data), True, 0x00030000, size, size, 0)


class Tray:
    """Call start() once. set_state(), notify() and stop() may be called from any thread."""

    def __init__(
        self,
        on_open: Callable[[], None],
        menu: Callable[[], list[MenuItem]],
        state: str = "disconnected",
        tip: str = "VoiceForever",
    ) -> None:
        self.on_open = on_open
        self.menu = menu
        self._state, self._tip = state, tip
        self._icons: dict[str, int] = {}
        self._work: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._wndproc = WNDPROC(self._proc)  # keep a reference: Windows calls it later
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._thread = threading.Thread(target=self._run, name="vf-tray", daemon=True)

    # ------------------------------------------------------------ any thread
    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def set_state(self, state: str, tip: str) -> None:
        self._later(lambda: self._update(state, tip))

    def notify(self, title: str, text: str, warning: bool = False) -> None:
        self._later(lambda: self._balloon(title, text, warning))

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            self._thread.join(3)

    def _later(self, work: Callable[[], None]) -> None:
        self._work.put(work)
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_WAKE, 0, 0)

    # ------------------------------------------------------------ tray thread
    def _run(self) -> None:
        try:
            instance = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=instance, lpszClassName=CLASS_NAME)
            user32.RegisterClassW(ctypes.byref(wc))
            # A hidden top-level window (not message-only) so it hears TaskbarCreated
            # and can put the icon back after Explorer restarts.
            self._hwnd = user32.CreateWindowExW(0, CLASS_NAME, "VoiceForever", 0, 0, 0, 0, 0, None, None, instance, None)
            size = user32.GetSystemMetrics(SM_CXSMICON) or 16
            self._icons = {name: make_icon(size, name) for name in DOTS}
            self._shell(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)
            user32.PostMessageW(self._hwnd, WM_WAKE, 0, 0)  # work queued before the window existed
        except Exception:
            log.exception("tray icon failed")
            return
        finally:
            self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for icon in self._icons.values():
            user32.DestroyIcon(icon)

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self._hwnd, uID=1, uFlags=flags)
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icons.get(self._state, 0)
        data.szTip = self._tip[:127]
        return data

    def _shell(self, action: int, flags: int, data: NOTIFYICONDATAW | None = None) -> None:
        data = data or self._data(flags)
        if not shell32.Shell_NotifyIconW(action, ctypes.byref(data)):
            log.debug("Shell_NotifyIcon(%d) failed", action)

    def _update(self, state: str, tip: str) -> None:
        self._state, self._tip = state, tip
        self._shell(NIM_MODIFY, NIF_ICON | NIF_TIP)

    def _balloon(self, title: str, text: str, warning: bool) -> None:
        data = self._data(NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = text[:255]
        data.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        self._shell(NIM_MODIFY, NIF_INFO, data)

    def _show_menu(self) -> None:
        items = self.menu()
        menu = user32.CreatePopupMenu()
        actions: dict[int, Callable[[], None]] = {}
        for item in items:
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            label, action = item
            command = len(actions) + 1
            actions[command] = action
            user32.AppendMenuW(menu, MF_STRING, command, label)
        user32.SetMenuDefaultItem(menu, 1, False)  # the first item, in bold
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self._hwnd)  # otherwise the menu does not close on an outside click
        flags = TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY
        chosen = user32.TrackPopupMenu(menu, flags, point.x, point.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if chosen in actions:
            self._safely(actions[chosen])

    @staticmethod
    def _safely(action: Callable[[], None]) -> None:
        try:
            action()
        except Exception:
            log.exception("tray action failed")

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TRAY:
            if lparam == WM_LBUTTONUP:
                self._safely(self.on_open)
            elif lparam == WM_RBUTTONUP:
                self._show_menu()
        elif msg == WM_WAKE:
            while not self._work.empty():
                self._safely(self._work.get())
        elif msg == self._taskbar_created:
            self._shell(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)
        elif msg == WM_CLOSE:
            self._shell(NIM_DELETE, 0)
            user32.DestroyWindow(hwnd)
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
        else:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0
