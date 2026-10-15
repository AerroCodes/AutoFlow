"""
target_window.py — lets a macro drive one specific window in the background.

Instead of moving the real mouse / pressing real keys (which always land in
whatever window is focused), input is posted straight into the target
window's message queue. The user can keep working in other windows while the
macro runs, with the target sitting behind them. Classic apps (Notepad, ...)
even take input while minimized; Chromium/Electron ones don't, so actions
wait until those are un-minimized.

Chromium/Electron apps (Discord, browsers, ...) ignore keyboard input while
they think they're inactive, so each burst of input is wrapped in a fake
"you're active now" / "you're inactive again" notification sent only to the
target. Real focus never moves — the user's own typing is unaffected.

Windows only. On other platforms list_windows() returns [] and every
TargetWindow action is a no-op that returns False.
"""

import ctypes
import os
import re
import sys
import time

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD), ("flags", wt.DWORD),
            ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND),
            ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
            ("rcCaret", wt.RECT),
        ]

    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsChild.argtypes = [wt.HWND, wt.HWND]
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                           ctypes.POINTER(ctypes.c_size_t)]
    user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
    user32.MapVirtualKeyW.restype = wt.UINT
    user32.VkKeyScanW.argtypes = [wt.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.FlashWindowEx.argtypes = [ctypes.c_void_p]
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WA_INACTIVE, WA_ACTIVE = 0, 1
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
SMTO_ABORTIFHUNG = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "space": 0x20, "delete": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
# Keys whose scan code lives on the extended (E0) half of the keyboard.
EXTENDED_KEYS = {0x2D, 0x2E, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28}

# How long the target gets to start handling input after the fake activation,
# and to finish handling it before it's told it's inactive again. Chromium
# forwards input to its renderer asynchronously, so this can't be zero.
ACTIVATE_SETTLE = 0.3
INPUT_SETTLE = 0.3
# Untouched for longer than WAKE_AFTER seconds, the target first gets a
# wake-up activation of WAKE_SETTLE seconds (see TargetWindow._burst).
WAKE_AFTER = 15.0
WAKE_SETTLE = 1.5
# The target turns a posted KEYDOWN into WM_CHAR at the *back* of its queue,
# so it must get to that key before more text is posted after it.
KEY_SETTLE = 0.03
# Pause after a Chromium window is un-minimized before touching it again.
RESTORE_GRACE = 3.0


def _lparam(value):
    # LPARAM is signed; key-up flags set bit 31, which must wrap on 32-bit Python.
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd):
    # UWP apps keep invisible "cloaked" windows around that would otherwise
    # clutter the picker.
    if dwmapi is None:
        return False
    cloaked = wt.DWORD()
    dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return bool(cloaked.value)


def _is_pickable(hwnd):
    return (user32.IsWindowVisible(hwnd)
            and not user32.GetWindow(hwnd, GW_OWNER)
            and not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW
            and not _is_cloaked(hwnd)
            and _window_text(hwnd).strip())


def list_windows():
    """Every normal top-level window the user could target, as dicts with
    `hwnd`, `title` and `exe`. AutoFlow's own windows are left out."""
    if not IS_WINDOWS:
        return []
    own_pid = os.getpid()
    found = []

    @WNDENUMPROC
    def callback(hwnd, _):
        if _is_pickable(hwnd):
            pid = _pid(hwnd)
            if pid != own_pid:
                found.append({"hwnd": hwnd, "title": _window_text(hwnd), "exe": _exe_name(pid)})
        return True

    user32.EnumWindows(callback, 0)
    return found


def stable_title(title):
    """`title` without a leading unread-count badge like "(3) " or "• " that
    apps such as Discord add and remove on their own."""
    return re.sub(r"^(\(\d+\+?\)|•)\s*", "", title).strip()


def _matches(title, exe, want_title, want_exe):
    if want_exe and exe.lower() != want_exe.lower():
        return False
    return want_title.lower() in title.lower()


def find_window(title, exe=None):
    """Handle of the window whose title contains `title` (case-insensitive),
    optionally limited to process `exe`. An exact title match wins over a
    partial one. Returns None if nothing matches."""
    if not IS_WINDOWS or not title:
        return None
    candidates = [w for w in list_windows() if _matches(w["title"], w["exe"], title, exe)]
    for w in candidates:
        if w["title"].lower() == title.lower():
            return w["hwnd"]
    return candidates[0]["hwnd"] if candidates else None


def flash_window(hwnd, count=3):
    """Blinks the window's taskbar button so the user can see which window
    a target resolved to, without touching the window itself."""
    if not IS_WINDOWS or not hwnd:
        return

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("hwnd", wt.HWND), ("dwFlags", wt.DWORD),
                    ("uCount", wt.UINT), ("dwTimeout", wt.DWORD)]

    FLASHW_ALL = 0x3
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL, count, 0)
    user32.FlashWindowEx(ctypes.byref(info))


def _send(hwnd, msg, wparam=0, lparam=0):
    # Sent synchronously, but never hangs the macro on a frozen target.
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, 1000, ctypes.byref(result))


def _fake_activate(hwnd):
    _send(hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
    _send(hwnd, WM_SETFOCUS, 0, 0)


def _fake_deactivate(hwnd):
    _send(hwnd, WM_KILLFOCUS, 0, 0)
    _send(hwnd, WM_ACTIVATE, WA_INACTIVE, 0)


def _post(hwnd, msg, wparam=0, lparam=0):
    user32.PostMessageW(hwnd, msg, wparam, _lparam(lparam))


def _key_lparam(vk, up):
    scan = user32.MapVirtualKeyW(vk, 0)
    lparam = 1 | (scan << 16)
    if vk in EXTENDED_KEYS:
        lparam |= 1 << 24
    if up:
        lparam |= (1 << 30) | (1 << 31)
    return lparam


def _is_chromium(hwnd):
    # Every Chromium/Electron top-level window has this class; newer Electron
    # (e.g. Discord) no longer has the Chrome_RenderWidgetHostHWND child.
    return _class_name(hwnd).startswith("Chrome_WidgetWin")


def blocked_by_minimize(hwnd):
    """True if the window is a minimized Chromium/Electron app (Discord,
    browsers): those ignore background input until un-minimized, and waking
    them up makes them jump in front of whatever the user is doing."""
    return IS_WINDOWS and bool(hwnd) and bool(user32.IsIconic(hwnd)) and _is_chromium(hwnd)


def _keyboard_target(hwnd):
    """The child window that actually takes keyboard input inside `hwnd`."""
    thread = user32.GetWindowThreadProcessId(hwnd, None)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.hwndFocus:
        if info.hwndFocus == hwnd or user32.IsChild(hwnd, info.hwndFocus):
            return info.hwndFocus
    # Focus is only reported for the foreground thread, so for a background
    # window guess: Chromium/Electron takes input on its render widget if it
    # still has one, else on the top-level window; anything else on its first
    # text box (Notepad, WordPad, editors, ...).
    if _is_chromium(hwnd):
        return user32.FindWindowExW(hwnd, None, "Chrome_RenderWidgetHostHWND", None) or hwnd
    editors = []

    @WNDENUMPROC
    def callback(child, _):
        cls = _class_name(child).lower()
        if user32.IsWindowVisible(child) and ("edit" in cls or "scintilla" in cls):
            editors.append(child)
            return False
        return True

    user32.EnumChildWindows(hwnd, callback, 0)
    return editors[0] if editors else hwnd


class TargetWindow:
    """A window chosen in AutoFlow that a macro can type into in the
    background. AutoFlow passes one to `run(stop_event, window)`.

    The window is looked up by title (case-insensitive "contains") and,
    optionally, by process name. If it disappears — closed, or its title
    changed (e.g. you switched to another Discord channel) — every action
    waits until it's back, or until the macro is stopped."""

    def __init__(self, title, exe=None, stop_event=None):
        self.title_match = title
        self.exe = exe or None
        self.stop_event = stop_event
        self._hwnd = None
        self._waiting = None  # why wait() is currently blocked, to log each reason once
        self._last_input = 0.0

    def __repr__(self):
        return f"TargetWindow({self.title_match!r}, exe={self.exe!r})"

    # -------------------------------------------------------------- lookup
    @property
    def hwnd(self):
        """Current window handle, or None if the window isn't open right now."""
        if not IS_WINDOWS:
            return None
        h = self._hwnd
        if h and user32.IsWindow(h) and _matches(_window_text(h), _exe_name(_pid(h)), self.title_match, self.exe):
            return h
        self._hwnd = find_window(self.title_match, self.exe)
        return self._hwnd

    @property
    def exists(self):
        return self.hwnd is not None

    @property
    def title(self):
        """The window's full current title ("" if it isn't open)."""
        h = self.hwnd
        return _window_text(h) if h else ""

    def wait(self, timeout=None):
        """Wait until the window is open and can take input. Returns its
        handle, or None if the macro was stopped or `timeout` seconds passed
        first."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            h = self.hwnd
            if not h:
                reason = "missing"
            elif blocked_by_minimize(h):
                reason = "minimized"
            else:
                reason = None
            if not reason:
                if self._waiting:
                    print(f"[target] '{self.title}' is ready — continuing.")
                    was_minimized = self._waiting == "minimized"
                    self._waiting = None
                    # Chromium fake-activated right after being un-minimized
                    # also jumps to the front, so give it a moment first.
                    if was_minimized and not self._sleep(RESTORE_GRACE):
                        return None
                    continue
                return h
            if self._waiting != reason:
                if reason == "minimized":
                    print(f"[target] '{_window_text(h)}' is minimized — apps like Discord ignore background "
                          f"input then. Un-minimize it (it can stay behind other windows); waiting...")
                else:
                    print(f"[target] Waiting for a window matching '{self.title_match}'"
                          f"{f' ({self.exe})' if self.exe else ''}...")
                self._waiting = reason
            if deadline is not None and time.time() >= deadline:
                return None
            if not self._sleep(0.25):
                return None

    def _sleep(self, seconds):
        """Sleep unless the macro gets stopped; False if it was."""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event is not None and self.stop_event.is_set():
                return False
            time.sleep(min(0.05, max(0.0, end - time.time())))
        return not (self.stop_event is not None and self.stop_event.is_set())

    # --------------------------------------------------------------- input
    def _burst(self, action):
        """Runs `action(hwnd)` while the target believes it's the active
        window. Returns False if the window couldn't be reached."""
        hwnd = self.wait()
        if not hwnd:
            return False
        foreground = user32.GetForegroundWindow() == hwnd
        if not foreground:
            if time.time() - self._last_input > WAKE_AFTER:
                # After a while in the background Discord puts the focus back
                # into its message box only lazily, so the first input would
                # get lost. A wake-up activation without input lets it settle.
                _fake_activate(hwnd)
                time.sleep(WAKE_SETTLE)
                _fake_deactivate(hwnd)
            _fake_activate(hwnd)
            time.sleep(ACTIVATE_SETTLE)
        try:
            action(hwnd)
            time.sleep(INPUT_SETTLE)
        finally:
            self._last_input = time.time()
            # The user may have switched to the target meanwhile — don't
            # yank it back out of focus then.
            if not foreground and user32.GetForegroundWindow() != hwnd:
                _fake_deactivate(hwnd)
        return True

    def type(self, text, interval=0.0):
        """Type `text` into the window. Use "\\n" or press("enter") for Enter.
        Returns True if it was delivered."""
        if not IS_WINDOWS:
            return False

        def action(hwnd):
            target = _keyboard_target(hwnd)
            for ch in text:
                if self.stop_event is not None and self.stop_event.is_set():
                    return
                if ch == "\n":
                    self._post_key(target, KEYS["enter"])
                else:
                    # astral chars (emoji) go as a UTF-16 surrogate pair
                    data = ch.encode("utf-16-le")
                    for i in range(0, len(data), 2):
                        _post(target, WM_CHAR, int.from_bytes(data[i:i + 2], "little"), 1)
                if interval:
                    time.sleep(interval)

        return self._burst(action)

    def press(self, key, times=1, interval=0.05):
        """Press a key: "enter", "tab", "esc", "backspace", "space", arrows
        ("up", "down", ...), "home", "end", "pageup", "pagedown", "delete",
        "f1".."f24", or any single character. Returns True if delivered.
        Key combos (ctrl+...) aren't supported in the background."""
        if not IS_WINDOWS:
            return False
        vk = self._vk(key)

        def action(hwnd):
            target = _keyboard_target(hwnd)
            for i in range(times):
                if self.stop_event is not None and self.stop_event.is_set():
                    return
                self._post_key(target, vk)
                if interval and i < times - 1:
                    time.sleep(interval)

        return self._burst(action)

    def send(self, text):
        """Type `text` and press Enter — e.g. send a chat message."""
        return self.type(text + "\n")

    @staticmethod
    def _vk(key):
        name = key.strip().lower()
        if name in KEYS:
            return KEYS[name]
        if len(key) == 1:
            scan = user32.VkKeyScanW(key)
            if scan != -1:
                return scan & 0xFF
        raise ValueError(f"Unknown key {key!r}")

    @staticmethod
    def _post_key(target, vk):
        # The target's own message loop turns KEYDOWN into the matching
        # WM_CHAR, exactly like a real key press.
        _post(target, WM_KEYDOWN, vk, _key_lparam(vk, False))
        _post(target, WM_KEYUP, vk, _key_lparam(vk, True))
        time.sleep(KEY_SETTLE)
