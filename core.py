"""Core engine for the Windows auto clicker.

The module intentionally contains no UI.  It can be imported by a Tkinter/Qt
front end and is safe to start/stop from hotkey callbacks.

Dependencies: ``pynput`` (for optional recording).  Click injection uses the
Windows ``SendInput`` API directly and therefore does not require admin rights
for normal desktop applications.
"""
from __future__ import annotations

import ctypes
import math
import platform
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple


if platform.system() == "Windows":
    _user32 = ctypes.windll.user32
    _ULONG_PTR = ctypes.c_size_t
    _HWND = ctypes.c_void_p

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
            ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
            ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
            ("biClrImportant", ctypes.c_ulong),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", ctypes.c_ulong * 3)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]

    # Preserve SendInput's thread-local error without changing how the other
    # user32 calls above/below expose their errors to ctypes.WinError().
    _send_input = ctypes.WinDLL("user32", use_last_error=True).SendInput
    _send_input.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    _send_input.restype = ctypes.c_uint

    _INPUT_MOUSE = 0
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010
    _MOUSEEVENTF_MIDDLEDOWN = 0x0020
    _MOUSEEVENTF_MIDDLEUP = 0x0040

    _user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.GetWindowTextLengthW.argtypes = [_HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [_HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [_HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [_HWND, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _user32.IsWindow.argtypes = [_HWND]
    _user32.IsWindow.restype = ctypes.c_bool
    _user32.IsWindowVisible.argtypes = [_HWND]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsWindowEnabled.argtypes = [_HWND]
    _user32.IsWindowEnabled.restype = ctypes.c_bool
    _user32.GetAncestor.argtypes = [_HWND, ctypes.c_uint]
    _user32.GetAncestor.restype = _HWND
    _user32.WindowFromPoint.argtypes = [_POINT]
    _user32.WindowFromPoint.restype = _HWND
    _user32.ScreenToClient.argtypes = [_HWND, ctypes.POINTER(_POINT)]
    _user32.ScreenToClient.restype = ctypes.c_bool
    _user32.ChildWindowFromPointEx.argtypes = [_HWND, _POINT, ctypes.c_uint]
    _user32.ChildWindowFromPointEx.restype = _HWND
    _user32.MapWindowPoints.argtypes = [_HWND, _HWND, ctypes.POINTER(_POINT), ctypes.c_uint]
    _user32.MapWindowPoints.restype = ctypes.c_int
    _user32.GetClientRect.argtypes = [_HWND, ctypes.POINTER(_RECT)]
    _user32.GetClientRect.restype = ctypes.c_bool
    _user32.PostMessageW.argtypes = [_HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.PostMessageW.restype = ctypes.c_bool
    _user32.GetDC.argtypes = [_HWND]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.ReleaseDC.argtypes = [_HWND, ctypes.c_void_p]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.PrintWindow.argtypes = [_HWND, ctypes.c_void_p, ctypes.c_uint]
    _user32.PrintWindow.restype = ctypes.c_bool
    _gdi32 = ctypes.windll.gdi32
    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_uint, ctypes.c_void_p,
                                 ctypes.POINTER(_BITMAPINFO), ctypes.c_uint]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteObject.restype = ctypes.c_bool
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.restype = ctypes.c_bool
else:
    _user32 = None
    _gdi32 = None
    _send_input = None


@dataclass(frozen=True)
class WindowInfo:
    """Stable metadata for a visible top-level Windows window."""

    hwnd: int
    title: str
    class_name: str
    process_id: int


def _window_text(hwnd: int) -> str:
    if _user32 is None:
        return ""
    length = max(0, int(_user32.GetWindowTextLengthW(hwnd)))
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_class(hwnd: int) -> str:
    if _user32 is None:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def get_window_info(hwnd: int) -> Optional[WindowInfo]:
    """Return metadata for ``hwnd``, or ``None`` if it is no longer valid."""
    if _user32 is None:
        raise OSError("window targeting is only supported on Windows")
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32.IsWindow(hwnd):
        return None
    process_id = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return WindowInfo(hwnd, _window_text(hwnd), _window_class(hwnd), int(process_id.value))


def list_windows(exclude_process_id: Optional[int] = None) -> List[WindowInfo]:
    """List titled, visible top-level windows in desktop z-order."""
    if _user32 is None:
        raise OSError("window targeting is only supported on Windows")
    windows: List[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, _HWND, ctypes.c_void_p)

    def visit(raw_hwnd, _lparam):
        hwnd = int(raw_hwnd or 0)
        if not hwnd or not _user32.IsWindowVisible(hwnd):
            return True
        info = get_window_info(hwnd)
        if (info is not None and info.title.strip()
                and info.process_id != exclude_process_id):
            windows.append(info)
        return True

    callback = callback_type(visit)
    if not _user32.EnumWindows(callback, None):
        raise ctypes.WinError()
    return windows


def window_from_screen_point(x: int, y: int) -> Optional[WindowInfo]:
    """Find the top-level window underneath a physical screen coordinate."""
    if _user32 is None:
        raise OSError("window targeting is only supported on Windows")
    hwnd = int(_user32.WindowFromPoint(_POINT(int(x), int(y))) or 0)
    if not hwnd:
        return None
    root = int(_user32.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT
    return get_window_info(root)


def screen_to_client(hwnd: int, x: int, y: int) -> Tuple[int, int]:
    """Convert a physical screen point to a window's client coordinates."""
    if _user32 is None:
        raise OSError("window targeting is only supported on Windows")
    point = _POINT(int(x), int(y))
    if not _user32.IsWindow(int(hwnd)) or not _user32.ScreenToClient(int(hwnd), ctypes.byref(point)):
        raise ctypes.WinError()
    return int(point.x), int(point.y)


def _message_target_at(hwnd: int, x: int, y: int) -> Tuple[int, int, int]:
    """Descend through child controls and return their local point."""
    current = int(hwnd)
    point = _POINT(int(x), int(y))
    flags = 0x0001 | 0x0002 | 0x0004  # skip invisible, disabled, transparent
    for _ in range(32):
        child = int(_user32.ChildWindowFromPointEx(current, point, flags) or 0)
        if not child or child == current:
            break
        _user32.MapWindowPoints(current, child, ctypes.byref(point), 1)
        current = child
    return current, int(point.x), int(point.y)


def _window_message_point(hwnd: int, x: int, y: int) -> Tuple[int, int]:
    """Validate a client point and pack it for a child window message."""
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32.IsWindow(hwnd):
        raise OSError("target window is no longer available")
    rect = _RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    x, y = int(x), int(y)
    if x < rect.left or y < rect.top or x >= rect.right or y >= rect.bottom:
        raise ValueError(f"client coordinate ({x}, {y}) is outside the target window")
    target, local_x, local_y = _message_target_at(hwnd, x, y)
    packed = ((local_y & 0xFFFF) << 16) | (local_x & 0xFFFF)
    return target, packed


def post_window_mouse_move(hwnd: int, x: int, y: int) -> None:
    """Post a mouse move inside a window without moving the real cursor."""
    if _user32 is None:
        raise OSError("background input is only supported on Windows")
    target, packed = _window_message_point(hwnd, x, y)
    if not _user32.PostMessageW(target, 0x0200, 0, packed):
        raise ctypes.WinError()


def _post_window_button(hwnd: int, x: int, y: int, button: str, *,
                        pressed: bool, double_click: bool = False) -> None:
    if _user32 is None:
        raise OSError("background input is only supported on Windows")
    target, packed = _window_message_point(hwnd, x, y)
    button = str(button).lower().replace("button.", "")
    messages = {
        "left": (0x0201, 0x0202, 0x0203, 0x0001),
        "right": (0x0204, 0x0205, 0x0206, 0x0002),
        "middle": (0x0207, 0x0208, 0x0209, 0x0010),
    }
    down, up, double, key_flag = messages.get(button, messages["left"])
    message = (double if double_click else down) if pressed else up
    if not _user32.PostMessageW(target, message, key_flag if pressed else 0, packed):
        raise ctypes.WinError()


def post_window_mouse_down(hwnd: int, x: int, y: int, button: str = "left") -> None:
    """Post a button-down message to a background window."""
    post_window_mouse_move(hwnd, x, y)
    _post_window_button(hwnd, x, y, button, pressed=True)


def post_window_mouse_up(hwnd: int, x: int, y: int, button: str = "left") -> None:
    """Post a button-up message to a background window."""
    _post_window_button(hwnd, x, y, button, pressed=False)


def post_window_click(hwnd: int, x: int, y: int, button: str = "left", *,
                      double_click: bool = False, duration: float = 0.0,
                      stop_event: Optional[threading.Event] = None) -> None:
    """Post a client-area click without moving the cursor or activating it.

    ``double_click=True`` posts the second (DBLCLK) half of a double click;
    callers should post a normal click first. A positive ``duration`` keeps
    the button down for applications that sample button state each frame.
    The down event is sent immediately; cancellation releases it early.
    """
    if _user32 is None:
        raise OSError("background clicking is only supported on Windows")
    duration = _click_duration(duration)
    if stop_event is not None and stop_event.is_set():
        return
    # Resolve once: controls may move or disappear between down and up.
    # Releasing at a newly hit-tested control could leave the first one held.
    target, packed = _window_message_point(hwnd, x, y)
    button = str(button).lower().replace("button.", "")
    messages = {
        "left": (0x0201, 0x0202, 0x0203, 0x0001),
        "right": (0x0204, 0x0205, 0x0206, 0x0002),
        "middle": (0x0207, 0x0208, 0x0209, 0x0010),
    }
    down, up, double, key_flag = messages.get(button, messages["left"])
    if not _user32.PostMessageW(target, 0x0200, 0, packed):
        raise ctypes.WinError()
    if stop_event is not None and stop_event.is_set():
        return
    if not _user32.PostMessageW(target, double if double_click else down, key_flag, packed):
        raise ctypes.WinError()
    try:
        _wait_click_duration(duration, stop_event)
    finally:
        if not _user32.PostMessageW(target, up, 0, packed):
            raise ctypes.WinError()


def capture_window_client(hwnd: int):
    """Capture a top-level window's client area using ``PrintWindow``.

    The returned value is a Pillow RGB image. ``PW_RENDERFULLCONTENT`` allows
    many desktop applications to render while covered or minimized, although
    exclusive GPU surfaces may still return black pixels.
    """
    if _user32 is None or _gdi32 is None:
        raise OSError("background window capture is only supported on Windows")
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32.IsWindow(hwnd):
        raise OSError("target window is no longer available")
    rect = _RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise ValueError("target window has an empty client area")
    window_dc = _user32.GetDC(hwnd)
    if not window_dc:
        raise ctypes.WinError()
    memory_dc = bitmap = previous = None
    try:
        memory_dc = _gdi32.CreateCompatibleDC(window_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not memory_dc or not bitmap:
            raise ctypes.WinError()
        previous = _gdi32.SelectObject(memory_dc, bitmap)
        if not previous:
            raise ctypes.WinError()
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        if not _user32.PrintWindow(hwnd, memory_dc, 0x00000001 | 0x00000002):
            raise OSError("target window did not provide a background image")
        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(
            ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32,
            0, width * height * 4, 0, 0, 0, 0,
        )
        pixels = ctypes.create_string_buffer(width * height * 4)
        # GetDIBits formally requires the bitmap not to be selected into a DC.
        _gdi32.SelectObject(memory_dc, previous)
        previous = None
        rows = _gdi32.GetDIBits(
            memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0,
        )
        if rows != height:
            raise ctypes.WinError()
        from PIL import Image
        return Image.frombuffer("RGB", (width, height), pixels, "raw", "BGRX", 0, 1).copy()
    finally:
        if previous and memory_dc:
            _gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if memory_dc:
            _gdi32.DeleteDC(memory_dc)
        _user32.ReleaseDC(hwnd, window_dc)


def _click_duration(duration: float) -> float:
    duration = float(duration)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("click duration must be a finite non-negative number")
    return duration


def _wait_click_duration(duration: float,
                         stop_event: Optional[threading.Event]) -> None:
    if duration > 0:
        if stop_event is None:
            time.sleep(duration)
        else:
            stop_event.wait(duration)


def send_click(button: str = "left", *, duration: float = 0.0,
               stop_event: Optional[threading.Event] = None) -> None:
    """Press immediately, optionally hold briefly, then release the button.

    The default duration preserves the normal clicker's maximum speed.
    A stop event interrupts the hold while still guaranteeing release.
    """
    duration = _click_duration(duration)
    if stop_event is not None and stop_event.is_set():
        return
    # Keep the button state balanced even when a platform call raises.  A
    # failed ``SendInput`` should never leave a physical button held down and
    # blocking the user's normal mouse input after the worker exits.
    try:
        send_mouse_down(button)
    except BaseException:
        # ``SendInput`` can report an error after the down event has already
        # reached the OS. Make a best-effort up call, but preserve the
        # original exception so the worker can report the actual failure.
        try:
            send_mouse_up(button)
        except Exception:
            pass
        raise
    try:
        _wait_click_duration(duration, stop_event)
    finally:
        send_mouse_up(button)


def _mouse_flags(button: str) -> Tuple[int, int]:
    """Return the SendInput down/up flags for a normalized mouse button."""
    if _user32 is None:
        raise OSError("mouse input is only supported on Windows")
    button = str(button).lower().replace("button.", "")
    if button == "right":
        return _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
    if button == "middle":
        return _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP
    return _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP


def _send_mouse_flag(flag: int) -> None:
    if _user32 is None:
        raise OSError("send_click is only supported on Windows")
    inp = _INPUT(type=_INPUT_MOUSE,
                 mi=_MOUSEINPUT(0, 0, 0, flag, 0, 0))
    ctypes.set_last_error(0)
    if _send_input(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
        # Windows does not reliably supply an error code when UIPI rejects
        # input. WinError(0) would misleadingly say the operation succeeded.
        raise OSError(
            "SendInput 未能发送鼠标输入（系统未提供错误码）；"
            "请检查目标窗口与连点器的权限是否一致。"
        )


def send_mouse_down(button: str = "left") -> None:
    """Press ``button`` at the current cursor location and keep it down."""
    down, _ = _mouse_flags(button)
    _send_mouse_flag(down)


def send_mouse_up(button: str = "left") -> None:
    """Release ``button`` at the current cursor location."""
    _, up = _mouse_flags(button)
    _send_mouse_flag(up)


@dataclass
class ClickSettings:
    interval: float = 0.1
    button: str = "left"
    count: int = 0  # 0 means unlimited


class ClickEngine:
    """Background click loop with race-free start/stop semantics.

    A fresh stop event is created for every run.  Reusing an event is subtly
    unsafe: if a previous worker takes longer than the stop timeout, clearing
    that event for a new run would allow the old worker to resume as well.
    """
    def __init__(self, settings: Optional[ClickSettings] = None,
                 on_count: Optional[Callable[[int], None]] = None,
                 on_error: Optional[Callable[[BaseException], None]] = None,
                 on_done: Optional[Callable[[], None]] = None):
        self.settings = settings or ClickSettings()
        self.on_count = on_count
        self.on_error = on_error
        self.on_done = on_done
        self._lock = threading.RLock()
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._count = 0
        self._generation = 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._count = 0
            self._generation += 1
            generation = self._generation
            stop = threading.Event()
            self._stop = stop
            # Snapshot settings for this run.  Editing fields in the UI while
            # clicking is active must not produce a half-updated loop.
            settings = ClickSettings(
                interval=self.settings.interval,
                button=self.settings.button,
                count=self.settings.count,
            )
            thread = threading.Thread(
                target=self._run,
                args=(stop, generation, settings),
                name="click-loop",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                if self._thread is thread and generation == self._generation:
                    self._thread = None
                    self._stop = None
            raise
        return True

    def stop(self) -> None:
        with self._lock:
            stop = self._stop
            t = self._thread
            if stop is not None:
                stop.set()
        if t and t is not threading.current_thread():
            t.join(timeout=1.0)
        # Keep a still-alive thread referenced.  ``start`` will refuse to
        # launch another worker until the old one has really exited.
        with self._lock:
            if self._thread is t and t is not None and not t.is_alive():
                self._thread = None
                self._stop = None

    def _run(self, stop: threading.Event, generation: int,
             settings: ClickSettings) -> None:
        interval = max(0.001, float(settings.interval))
        limit = max(0, int(settings.count))
        next_tick = time.perf_counter()
        try:
            while not stop.is_set():
                with self._lock:
                    current_count = self._count
                if limit and current_count >= limit:
                    break
                send_click(settings.button)
                with self._lock:
                    # A stale worker must never update a newer run's count.
                    if generation != self._generation:
                        break
                    self._count += 1
                    current_count = self._count
                if self.on_count:
                    try:
                        self.on_count(current_count)
                    except Exception as exc:
                        # UI callbacks should not silently kill the click
                        # worker.  Report the callback failure if requested.
                        if self.on_error:
                            self.on_error(exc)
                next_tick += interval
                stop.wait(max(0.0, next_tick - time.perf_counter()))
        except Exception as exc:
            if self.on_error:
                try:
                    self.on_error(exc)
                except Exception:
                    pass
        finally:
            with self._lock:
                is_current = (
                    self._thread is threading.current_thread()
                    and generation == self._generation
                )
                if is_current:
                    self._thread = None
                    self._stop = None
            if is_current and self.on_done:
                try:
                    self.on_done()
                except Exception:
                    pass


@dataclass
class RecordedEvent:
    kind: str  # "move", "click"
    t: float
    x: int
    y: int
    button: str = "left"
    pressed: bool = True


class Recorder:
    """Capture mouse movement/click events using pynput.

    ``start`` and ``stop`` may be called from the UI thread.  Timestamps are
    relative to recording start, making the resulting list directly replayable.
    """
    def __init__(self):
        self.events: List[RecordedEvent] = []
        self._listener = None
        self._started = 0.0
        self._lock = threading.RLock()
        self._recording = False
        self._last_move_t = -1.0
        self._last_move_xy: Optional[Tuple[int, int]] = None

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    def snapshot(self) -> List[RecordedEvent]:
        """Return a stable copy suitable for displaying or replaying."""
        with self._lock:
            return list(self.events)

    def start(self, include_moves: bool = True, move_interval: float = 0.016) -> None:
        if self.recording:
            self.stop()
        from pynput import mouse
        move_interval = max(0.0, float(move_interval))
        with self._lock:
            self.events.clear()
            self._started = time.perf_counter()
            self._recording = True
            self._last_move_t = -1.0
            self._last_move_xy = None

        def stamp() -> float:
            return time.perf_counter() - self._started

        def on_move(x, y):
            if not include_moves:
                return
            x, y, now = int(x), int(y), stamp()
            with self._lock:
                if not self._recording:
                    return
                if self._last_move_xy == (x, y) or (
                    self._last_move_t >= 0 and now - self._last_move_t < move_interval
                ):
                    return
                self._last_move_t = now
                self._last_move_xy = (x, y)
                self.events.append(RecordedEvent("move", now, x, y))

        def on_click(x, y, button, pressed):
            event = RecordedEvent("click", stamp(), int(x), int(y),
                                  getattr(button, "name", str(button)), pressed)
            with self._lock:
                if self._recording:
                    self.events.append(event)

        listener = mouse.Listener(on_move=on_move, on_click=on_click)
        with self._lock:
            self._listener = listener
        try:
            listener.start()
        except Exception:
            with self._lock:
                self._recording = False
                self._listener = None
            raise

    def stop(self) -> List[RecordedEvent]:
        with self._lock:
            listener = self._listener
            self._recording = False
            self._listener = None
        if listener:
            listener.stop()
            listener.join(timeout=1)
        with self._lock:
            return list(self.events)

    def replay(self, events: Optional[Iterable[RecordedEvent]] = None,
               speed: float = 1.0, stop: Optional[threading.Event] = None) -> None:
        """Replay events synchronously; call from a worker thread."""
        if events is None:
            with self._lock:
                events = list(self.events)
        else:
            events = list(events)
        # Accept both RecordedEvent objects and the JSON dictionaries emitted
        # by the UI.  Normalising here keeps persistence format details out of
        # the actual replay loop.
        normalised: List[RecordedEvent] = []
        for raw in events:
            if isinstance(raw, RecordedEvent):
                event = raw
            elif isinstance(raw, dict):
                kind = str(raw.get("kind", raw.get("type", "move"))).lower()
                button = str(raw.get("button", "left")).replace("Button.", "").lower()
                event = RecordedEvent(
                    kind=kind,
                    t=float(raw.get("t", 0.0)),
                    x=int(raw.get("x", 0)),
                    y=int(raw.get("y", 0)),
                    button=button,
                    pressed=bool(raw.get("pressed", True)),
                )
            else:
                raise TypeError(f"unsupported recorded event: {type(raw).__name__}")
            if not math.isfinite(event.t) or event.t < 0:
                raise ValueError("recorded event timestamp must be a finite non-negative number")
            if event.kind not in {"move", "click"}:
                raise ValueError(f"unsupported recorded event kind: {event.kind!r}")
            normalised.append(event)
        events = sorted(normalised, key=lambda event: event.t)
        speed = max(0.01, float(speed))
        stop = stop or threading.Event()
        if _user32 is None:
            raise OSError("replay is only supported on Windows")
        for i, ev in enumerate(events):
            if stop.is_set():
                break
            prev = events[i - 1].t if i else 0.0
            if stop.wait(max(0.0, (ev.t - prev) / speed)):
                break
            if not _user32.SetCursorPos(int(ev.x), int(ev.y)):
                raise ctypes.WinError()
            if ev.kind == "click" and ev.pressed:
                send_click(ev.button)
