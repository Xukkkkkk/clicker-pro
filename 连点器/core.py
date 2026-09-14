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

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]

    _INPUT_MOUSE = 0
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010
    _MOUSEEVENTF_MIDDLEDOWN = 0x0020
    _MOUSEEVENTF_MIDDLEUP = 0x0040
else:
    _user32 = None


def send_click(button: str = "left") -> None:
    """Inject a single mouse click at the current cursor location."""
    send_mouse_down(button)
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
    if _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) != 1:
        raise ctypes.WinError()


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
