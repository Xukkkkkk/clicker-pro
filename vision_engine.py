"""Screen image recognition and auto-clicking engine.

The module is deliberately independent from the Tkinter UI.  A caller can
add one or more :class:`TemplateSpec` objects, preview with
``scan_once()``, or run a background scanner with ``start()``.  OpenCV and a
screen-capture backend are imported lazily, so importing this module does not
make the existing clicker unusable when the optional vision dependencies have
not been installed yet.

Typical usage::

    spec = TemplateSpec(name="confirm", path="C:/images/confirm.png",
                        threshold=0.88, cooldown=0.5)
    engine = VisionEngine([spec], auto_click=True,
                          on_match=lambda match: print(match.center))
    engine.start()
    ...
    engine.stop()

Coordinates in ``region`` and in :class:`VisionMatch` are absolute screen
coordinates in the Windows virtual desktop.  A region is ``(left, top,
width, height)``.  ``auto_click`` moves the cursor to the center of a match
and executes that template's action plan (repeated clicks, holds, waits and
follow-up mouse buttons); applications that need their own click policy can
leave it disabled and use ``on_match`` instead.
"""

from __future__ import annotations

import ctypes
import math
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple, Union


PathLike = Union[str, os.PathLike[str], Path]
Region = Tuple[int, int, int, int]


class VisionError(RuntimeError):
    """Base class for errors raised by the vision engine."""


class VisionDependencyError(VisionError):
    """Raised when OpenCV/numpy or a capture backend is unavailable."""


class TemplateLoadError(VisionError):
    """Raised when a template image cannot be loaded or is invalid."""


class CaptureError(VisionError):
    """Raised when taking a screenshot fails."""


@dataclass(frozen=True)
class TemplateAction:
    """One mouse action performed after a template is matched.

    ``kind`` may be ``"click"``, ``"hold"`` or ``"wait"``.  Click
    actions can be repeated with ``count`` and spaced with ``interval``;
    hold actions press a button, keep it down for ``duration`` seconds and
    then release it.  The dataclass is deliberately JSON-friendly through
    :meth:`as_dict`, while accepting the same plain dictionaries used by the
    UI/configuration file via :func:`normalise_actions`.

    A single legacy ``TemplateSpec(button="right")`` is represented as one
    click action automatically, so existing configurations do not need to be
    migrated.
    """

    kind: str = "click"
    button: str = "left"
    count: int = 1
    interval: float = 0.08
    duration: float = 0.0

    def __post_init__(self) -> None:
        kind = _normalise_action_kind(self.kind)
        button = _normalise_button(self.button)
        try:
            count = int(self.count)
        except (TypeError, ValueError):
            count = 1
        try:
            interval = float(self.interval)
        except (TypeError, ValueError):
            interval = 0.08
        try:
            duration = float(self.duration)
        except (TypeError, ValueError):
            duration = 0.0
        if not math.isfinite(interval):
            interval = 0.08
        if not math.isfinite(duration):
            duration = 0.0
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "button", button)
        # A corrupt config should not be able to create an effectively
        # unbounded inner loop.  The UI uses the friendlier 1-999 range; the
        # engine allows a little more for programmatic callers.
        object.__setattr__(self, "count", min(10000, max(1, count)))
        object.__setattr__(self, "interval", max(0.0, interval))
        object.__setattr__(self, "duration", max(0.0, duration))

    @property
    def type(self) -> str:
        """Alias useful to callers that use ``type`` in JSON records."""
        return self.kind

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "button": self.button,
            "count": self.count,
            "interval": self.interval,
            "duration": self.duration,
        }


def _normalise_action_kind(value: Any) -> str:
    text = str(value or "click").strip().lower().replace("-", "_")
    aliases = {
        "tap": "click", "press": "click", "mouse_click": "click",
        "点击": "click", "单击": "click", "多次点击": "click",
        "long_press": "hold", "longpress": "hold", "press_hold": "hold",
        "长按": "hold", "按住": "hold", "等待": "wait", "延时": "wait",
        "delay": "wait", "sleep": "wait",
    }
    canonical = aliases.get(text, text)
    return canonical if canonical in {"click", "hold", "wait"} else "click"


def normalise_actions(
    actions: Any = None,
    *,
    button: Any = "left",
    click_count: Any = 1,
    click_interval: Any = 0.08,
    hold_duration: Any = 0.0,
) -> tuple[TemplateAction, ...]:
    """Convert UI/configuration values into a validated action plan.

    ``actions`` may be a single mapping, a sequence of mappings or
    :class:`TemplateAction` objects.  When it is omitted, the legacy fields
    are used.  A positive ``hold_duration`` creates a hold action; otherwise
    ``click_count`` creates repeated clicks.  Invalid numeric values are
    clamped to safe defaults instead of making an old config unloadable.
    """

    if actions is None:
        try:
            duration = float(hold_duration)
        except (TypeError, ValueError):
            duration = 0.0
        if not math.isfinite(duration) or duration < 0:
            duration = 0.0
        if duration > 0:
            return (TemplateAction("hold", _normalise_button(button), 1,
                                   0.0, duration),)
        return (TemplateAction("click", _normalise_button(button), click_count,
                               click_interval, 0.0),)

    if isinstance(actions, Mapping) or isinstance(actions, TemplateAction):
        actions = [actions]
    try:
        raw_actions = list(actions)
    except (TypeError, ValueError):
        raw_actions = []
    plan: list[TemplateAction] = []
    for raw in raw_actions:
        if isinstance(raw, TemplateAction):
            plan.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        data = dict(raw)
        kind = data.get("kind", data.get("type", data.get("action", "click")))
        action_button = data.get("button", data.get("mouse_button", button))
        count = data.get("count", data.get("clicks", data.get("repeat", 1)))
        interval = data.get("interval", data.get("gap", data.get("click_interval", 0.08)))
        duration = data.get("duration", data.get("hold_duration", data.get("press_duration", 0.0)))
        plan.append(TemplateAction(kind, action_button, count, interval, duration))
    # An empty/malformed sequence should preserve the old one-click behavior.
    return tuple(plan) or (TemplateAction("click", _normalise_button(button),
                                           click_count, click_interval, 0.0),)


def _wait_interruptible(seconds: float, stop_event: Optional[threading.Event] = None,
                        sleep_fn: Callable[[float], None] = time.sleep) -> bool:
    """Wait for ``seconds``; return ``True`` when a stop was requested."""
    seconds = max(0.0, float(seconds))
    if seconds <= 0:
        return bool(stop_event is not None and stop_event.is_set())
    if stop_event is not None:
        return bool(stop_event.wait(seconds))
    sleep_fn(seconds)
    return False


def execute_template_actions(
    actions: Any,
    *,
    x: int = 0,
    y: int = 0,
    stop_event: Optional[threading.Event] = None,
    move_fn: Optional[Callable[[int, int], None]] = None,
    click_fn: Optional[Callable[[str], None]] = None,
    mouse_down_fn: Optional[Callable[[str], None]] = None,
    mouse_up_fn: Optional[Callable[[str], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Execute a template's action plan and return completed click count.

    The low-level functions are injectable for tests and alternate frontends.
    A hold always releases its button in a ``finally`` block, including when
    the scanner is stopped or a wait is interrupted.  ``move_fn`` is called
    once before the first action when supplied.
    """
    plan = normalise_actions(actions)
    if stop_event is not None and stop_event.is_set():
        return 0
    if move_fn is not None:
        move_fn(int(x), int(y))
    if click_fn is None or mouse_down_fn is None or mouse_up_fn is None:
        try:
            from core import send_click as _send_click, send_mouse_down as _send_down, send_mouse_up as _send_up
        except ImportError as exc:  # pragma: no cover - optional runtime dep
            raise VisionDependencyError("无法加载鼠标点击模块 core.py") from exc
        click_fn = click_fn or _send_click
        mouse_down_fn = mouse_down_fn or _send_down
        mouse_up_fn = mouse_up_fn or _send_up
    completed = 0
    for action in plan:
        if stop_event is not None and stop_event.is_set():
            break
        kind = action.kind
        if kind == "wait":
            wait_for = action.duration if action.duration > 0 else action.interval
            if _wait_interruptible(wait_for, stop_event, sleep_fn):
                break
            continue
        if kind == "hold":
            pressed = False
            try:
                try:
                    mouse_down_fn(action.button)
                except BaseException:
                    # A platform call may fail after delivering the down
                    # event. Attempt a release while retaining the original
                    # exception for the caller.
                    try:
                        mouse_up_fn(action.button)
                    except Exception:
                        pass
                    raise
                pressed = True
                if _wait_interruptible(action.duration, stop_event, sleep_fn):
                    break
                completed += 1
            finally:
                if pressed:
                    mouse_up_fn(action.button)
            continue
        for index in range(max(1, action.count)):
            if stop_event is not None and stop_event.is_set():
                return completed
            click_fn(action.button)
            completed += 1
            if index + 1 < action.count and _wait_interruptible(action.interval, stop_event, sleep_fn):
                return completed
    return completed


@dataclass
class TemplateSpec:
    """A template image and the matching/click policy for that image.

    ``path`` can point to any image format understood by OpenCV.  ``image``
    may instead be a numpy array or a PIL image, which is useful for tests and
    callers that already loaded an uploaded file.  Supplying both is allowed;
    ``image`` takes precedence.

    ``id`` is optional and only needs to be unique among the templates.  When
    omitted, ``name`` (then the path) is used as the stable identifier.
    ``region`` is an absolute screen rectangle and ``max_matches`` controls
    how many distinct occurrences of this template may be reported per scan.
    """

    name: str = ""
    path: Optional[PathLike] = None
    image: Any = field(default=None, repr=False)
    threshold: float = 0.86
    cooldown: float = 0.35
    button: str = "left"
    enabled: bool = True
    region: Optional[Region] = None
    grayscale: bool = False
    max_matches: int = 1
    # ``click`` can disable auto-clicking for one template while the engine's
    # global ``auto_click`` setting remains enabled.
    click: bool = True
    id: Optional[str] = None
    # Compatibility spelling used by the first UI integration draft.  When
    # supplied, this takes precedence over ``id``/``name``.
    template_id: Optional[str] = None
    # Optional per-template action plan.  ``None`` keeps the legacy behavior
    # and derives one action from ``button``/``click_count``/``hold_duration``.
    actions: Optional[Sequence[Union[TemplateAction, Mapping[str, Any]]]] = None
    click_count: int = 1
    click_interval: float = 0.08
    hold_duration: float = 0.0

    @property
    def key(self) -> str:
        """Return the identifier used in callbacks and cooldown tracking."""
        value = self.template_id or self.id or self.name
        if not value and self.path is not None:
            value = Path(self.path).stem
        return str(value or "template")

    def normalised_region(self) -> Optional[Region]:
        if self.region is None:
            return None
        if len(self.region) != 4:
            raise ValueError("template region must be (left, top, width, height)")
        left, top, width, height = (int(v) for v in self.region)
        if width <= 0 or height <= 0:
            raise ValueError("template region width and height must be positive")
        return left, top, width, height

    @property
    def action_plan(self) -> tuple[TemplateAction, ...]:
        """Return the validated action sequence for this template."""
        return normalise_actions(
            self.actions,
            button=self.button,
            click_count=self.click_count,
            click_interval=self.click_interval,
            hold_duration=self.hold_duration,
        )


@dataclass(frozen=True)
class VisionMatch:
    """One template occurrence found on screen."""

    template_id: str
    template_name: str
    score: float
    x: int
    y: int
    width: int
    height: int
    center_x: int
    center_y: int
    timestamp: float
    button: str = "left"
    threshold: float = 0.86
    # Snapshot of the matched template's action plan.  Keeping it on the
    # result lets UI callbacks execute the exact plan that was active when
    # the scan happened, even if the user edits the row while a worker is
    # finishing its current cycle.
    actions: tuple[TemplateAction, ...] = ()

    @property
    def name(self) -> str:
        return self.template_name

    @property
    def center(self) -> Tuple[int, int]:
        return self.center_x, self.center_y

    @property
    def position(self) -> Tuple[int, int]:
        return self.center

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation for logs/UI tables."""
        return {
            "template_id": self.template_id,
            "template_name": self.template_name,
            "score": round(float(self.score), 6),
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "timestamp": self.timestamp,
            "button": self.button,
            "threshold": self.threshold,
            "actions": [action.as_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class ScreenFrame:
    """A captured image and its origin in virtual-screen coordinates."""

    image: Any
    origin_x: int = 0
    origin_y: int = 0
    timestamp: float = 0.0


def _optional_backends():
    """Import numpy/OpenCV only when recognition is actually requested."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise VisionDependencyError(
            "图片识别需要安装 opencv-python 和 numpy。请运行 "
            "python -m pip install opencv-python numpy"
        ) from exc
    return cv2, np


def _normalise_button(value: Any) -> str:
    value = str(value or "left").lower().replace("button.", "")
    value = {
        "左键": "left", "左 click": "left", "left click": "left",
        "右键": "right", "右 click": "right", "right click": "right",
        "中键": "middle", "中 click": "middle", "middle click": "middle",
    }.get(value, value)
    return value if value in {"left", "right", "middle"} else "left"


def _validate_threshold(value: Any, default: float = 0.86) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return min(1.0, max(0.0, number))


def _validate_region(value: Optional[Sequence[Any]]) -> Optional[Region]:
    if value is None:
        return None
    if len(value) != 4:
        raise ValueError("region must be (left, top, width, height)")
    left, top, width, height = (int(v) for v in value)
    if width <= 0 or height <= 0:
        raise ValueError("region width and height must be positive")
    return left, top, width, height


def _virtual_screen_origin() -> Tuple[int, int]:
    """Get the origin of the Windows virtual desktop, including negatives."""
    if platform.system() != "Windows":
        return 0, 0
    try:
        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
    except Exception:
        return 0, 0


class ScreenCapturer:
    """Capture the virtual desktop using mss when available, PIL otherwise."""

    def __init__(self, prefer_mss: bool = True):
        self.prefer_mss = bool(prefer_mss)
        self._mss = None
        self._mss_error: Optional[BaseException] = None
        self._lock = threading.Lock()

    def _get_mss(self):
        if self._mss is not None:
            return self._mss
        try:
            import mss  # type: ignore

            self._mss = mss.mss()
            return self._mss
        except Exception as exc:  # pragma: no cover - optional backend
            self._mss_error = exc
            return None

    def capture(self, region: Optional[Region] = None) -> ScreenFrame:
        """Return a BGR numpy frame and its absolute screen origin."""
        cv2, np = _optional_backends()
        # mss is substantially faster for a tight polling loop.  Keep one
        # instance per capturer because creating it per frame leaks handles on
        # some Windows versions.
        if self.prefer_mss:
            with self._lock:
                grabber = self._get_mss()
                if grabber is not None:
                    try:
                        if region is None:
                            monitor = dict(grabber.monitors[0])
                        else:
                            left, top, width, height = region
                            monitor = {"left": left, "top": top,
                                       "width": width, "height": height}
                        shot = grabber.grab(monitor)
                        # mss returns BGRA; OpenCV matching uses BGR.
                        image = np.asarray(shot, dtype=np.uint8)
                        if image.ndim == 3 and image.shape[2] >= 3:
                            image = image[:, :, :3].copy()
                        return ScreenFrame(
                            image=image,
                            origin_x=int(monitor["left"]),
                            origin_y=int(monitor["top"]),
                            timestamp=time.time(),
                        )
                    except Exception as exc:
                        # Fall through to PIL; mss can fail when a monitor is
                        # disconnected while the app is running.
                        self._mss_error = exc

        try:
            from PIL import ImageGrab  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VisionDependencyError(
                "屏幕截图需要 Pillow；请运行 python -m pip install Pillow"
            ) from exc

        try:
            if region is None:
                image_pil = ImageGrab.grab(all_screens=True)
                origin_x, origin_y = _virtual_screen_origin()
            else:
                left, top, width, height = region
                image_pil = ImageGrab.grab(
                    bbox=(left, top, left + width, top + height),
                    all_screens=True,
                )
                origin_x, origin_y = left, top
            rgb = np.asarray(image_pil.convert("RGB"), dtype=np.uint8)
            image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            return ScreenFrame(image=image, origin_x=origin_x,
                               origin_y=origin_y, timestamp=time.time())
        except Exception as exc:
            raise CaptureError(f"屏幕截图失败：{exc}") from exc


def _read_image(source: Any, cv2: Any, np: Any) -> Any:
    """Load a template from a PIL image, numpy array, or Unicode path."""
    if source is None:
        return None
    # A PIL Image (or another object exposing convert/size) is converted via
    # numpy without requiring cv2.imdecode to understand its path encoding.
    if hasattr(source, "convert") and hasattr(source, "size"):
        try:
            rgb = np.asarray(source.convert("RGB"), dtype=np.uint8)
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as exc:
            raise TemplateLoadError(f"无法读取模板图片：{exc}") from exc
    if isinstance(source, (str, os.PathLike, Path)):
        path = Path(source)
        if not path.is_file():
            raise TemplateLoadError(f"模板文件不存在：{path}")
        try:
            # imread has trouble with non-ASCII Windows paths.  Reading bytes
            # and decoding them works consistently for Chinese filenames.
            data = np.fromfile(str(path), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception as exc:
            raise TemplateLoadError(f"无法读取模板文件 {path}：{exc}") from exc
        if image is None:
            raise TemplateLoadError(f"无法解析模板图片：{path}")
        return image
    try:
        array = np.asarray(source)
    except Exception as exc:
        raise TemplateLoadError(f"不支持的模板图片类型：{type(source).__name__}") from exc
    if array.size == 0 or array.ndim not in (2, 3):
        raise TemplateLoadError("模板图片必须是非空二维或三维数组")
    if array.dtype != np.uint8:
        # Float images are common in tests and are safe to convert when they
        # already use the normal 0..1 range.
        if np.issubdtype(array.dtype, np.floating) and float(np.nanmax(array)) <= 1.0:
            array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
        else:
            array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 2:
        # Keep a grayscale source as-is; ``scan_once`` converts it to BGR for
        # colour matching or back to gray when ``spec.grayscale`` is enabled.
        return np.ascontiguousarray(array)
    if array.ndim == 3 and array.shape[2] == 4:
        array = cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    return np.ascontiguousarray(array)


def _as_bgr(image: Any, cv2: Any, np: Any) -> Any:
    """Normalise custom capture output to a contiguous BGR array."""
    if hasattr(image, "convert") and hasattr(image, "size"):
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    array = np.asarray(image)
    if array.size == 0 or array.ndim not in (2, 3):
        raise CaptureError("截图结果必须是非空二维或三维数组")
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 3 and array.shape[2] == 4:
        array = array[:, :, :3]
    if array.ndim == 2:
        # ImageGrab/mss normally produce colour frames, but this conversion
        # keeps custom test capture providers and headless integrations safe.
        array = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(array)


def _crop_absolute(frame: Any, origin_x: int, origin_y: int,
                   region: Optional[Region], np: Any) -> tuple[Any, int, int]:
    """Crop an absolute region and return image plus its new origin."""
    if region is None:
        return frame, int(origin_x), int(origin_y)
    left, top, width, height = region
    frame_h, frame_w = int(frame.shape[0]), int(frame.shape[1])
    rel_left = left - int(origin_x)
    rel_top = top - int(origin_y)
    x0 = max(0, rel_left)
    y0 = max(0, rel_top)
    x1 = min(frame_w, rel_left + width)
    y1 = min(frame_h, rel_top + height)
    if x1 <= x0 or y1 <= y0:
        return frame[0:0, 0:0], int(origin_x), int(origin_y)
    return frame[y0:y1, x0:x1], int(origin_x) + x0, int(origin_y) + y0


def _rect_iou(a: tuple[int, int, int, int],
              b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union else 0.0


@dataclass
class _PreparedTemplate:
    spec: TemplateSpec
    image: Any
    width: int
    height: int
    preferred_scale: float = 1.0
    scale_cursor: int = 0


class VisionEngine:
    """Multi-template screen matcher with optional automatic clicking.

    All callbacks are invoked on the scanner worker when ``start`` is used.
    A Tkinter caller should marshal UI updates with ``root.after``.  The
    ``capture_fn`` hook is intentionally public and makes deterministic tests
    possible; it may return a :class:`ScreenFrame`, an image, or
    ``(image, origin_x, origin_y)``.
    """

    def __init__(
        self,
        templates: Optional[Iterable[TemplateSpec]] = None,
        *,
        interval: float = 0.12,
        threshold: float = 0.86,
        cooldown: float = 0.35,
        region: Optional[Region] = None,
        auto_click: bool = False,
        click_fn: Optional[Callable[[VisionMatch], None]] = None,
        capture_fn: Optional[Callable[..., Any]] = None,
        on_match: Optional[Callable[[VisionMatch], None]] = None,
        on_scan: Optional[Callable[[list[VisionMatch]], None]] = None,
        on_cycle: Optional[Callable[[list[VisionMatch]], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
        prefer_mss: bool = True,
        exclude_regions: Optional[Callable[[], Sequence[Region]]] = None,
        immediate_click: bool = False,
    ):
        self.interval = max(0.01, float(interval))
        self.threshold = _validate_threshold(threshold)
        self.cooldown = max(0.0, float(cooldown))
        self.region = _validate_region(region)
        self.auto_click = bool(auto_click)
        self.click_fn = click_fn
        self.capture_fn = capture_fn
        self.on_match = on_match
        # ``on_cycle`` is an alias used by some integrations; if both are
        # provided, invoke both in the order supplied here (on_scan first).
        self.on_scan = on_scan
        self.on_cycle = on_cycle
        self.on_error = on_error
        self.on_done = on_done
        self.capturer = ScreenCapturer(prefer_mss=prefer_mss)
        self.exclude_regions = exclude_regions
        self.immediate_click = bool(immediate_click)
        self.last_scan_details: list[dict[str, Any]] = []
        self.last_scan_ms = 0.0
        self.last_capture_ms = 0.0
        self.last_dispatch_ms = 0.0
        self._next_template = 0
        self._scale_template = 0

        self._lock = threading.RLock()
        self._templates: list[TemplateSpec] = []
        self._prepared: dict[int, _PreparedTemplate] = {}
        self._last_fired: dict[str, list[tuple[int, int, float]]] = {}
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._generation = 0
        self.last_error: Optional[BaseException] = None
        if templates:
            for template in templates:
                self.add_template(template)

    @property
    def templates(self) -> list[TemplateSpec]:
        with self._lock:
            return list(self._templates)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def stop_event(self) -> Optional[threading.Event]:
        """The event used by the current worker, if scanning is active.

        The returned event is read-only by convention; callers may set it for
        cooperative shutdown, or call :meth:`stop` to also join the worker.
        """
        with self._lock:
            return self._stop

    @property
    def scan_interval(self) -> float:
        """Alias for ``interval`` used by older UI integrations."""
        return self.interval

    @scan_interval.setter
    def scan_interval(self, value: float) -> None:
        self.interval = max(0.01, float(value))

    def add_template(self, template: TemplateSpec) -> TemplateSpec:
        if isinstance(template, Mapping):
            raw = dict(template)
            # Accept the plain dictionaries emitted by vision_ui.py.  Unknown
            # presentation-only fields are intentionally ignored.
            allowed = {
                "name", "path", "image", "threshold", "cooldown", "button",
                "enabled", "region", "grayscale", "max_matches", "click",
                "id", "template_id", "actions", "click_count", "click_interval",
                "hold_duration",
            }
            template = TemplateSpec(**{key: value for key, value in raw.items()
                                       if key in allowed})
        if not isinstance(template, TemplateSpec):
            raise TypeError("template must be a TemplateSpec")
        # Validate user-editable values early while keeping image loading lazy.
        template.threshold = _validate_threshold(template.threshold, self.threshold)
        template.cooldown = max(0.0, float(template.cooldown))
        template.button = _normalise_button(template.button)
        template.region = template.normalised_region()
        template.max_matches = max(1, int(template.max_matches))
        # Validate the simple action fields eagerly.  The full sequence is
        # normalised here as well so malformed dictionaries are harmless and
        # the scanner thread never has to parse UI input.
        try:
            template.click_count = max(1, int(template.click_count))
        except (TypeError, ValueError):
            template.click_count = 1
        try:
            template.click_interval = float(template.click_interval)
        except (TypeError, ValueError):
            template.click_interval = 0.08
        if not math.isfinite(template.click_interval):
            template.click_interval = 0.08
        template.click_interval = max(0.0, template.click_interval)
        try:
            template.hold_duration = float(template.hold_duration)
        except (TypeError, ValueError):
            template.hold_duration = 0.0
        if not math.isfinite(template.hold_duration):
            template.hold_duration = 0.0
        template.hold_duration = max(0.0, template.hold_duration)
        # Materialise a tuple so callers can safely mutate their source list
        # after adding a template.  Keep ``None`` for the legacy form to make
        # persistence output compact and backwards compatible.
        if template.actions is not None:
            template.actions = template.action_plan
        if template.image is None and template.path is None:
            raise ValueError("template needs an image or path")
        with self._lock:
            key = template.key
            existing = {item.key for item in self._templates}
            if key in existing:
                # Stable unique IDs are useful when a user uploads two files
                # with the same basename.
                unique_key = f"{key}-{uuid.uuid4().hex[:6]}"
                if template.template_id:
                    template.template_id = unique_key
                else:
                    template.id = unique_key
            self._templates.append(template)
        return template

    def remove_template(self, template_id: str) -> bool:
        key = str(template_id)
        with self._lock:
            for index, template in enumerate(self._templates):
                if template.key == key:
                    self._templates.pop(index)
                    self._prepared.pop(id(template), None)
                    self._last_fired.pop(key, None)
                    return True
        return False

    def clear_templates(self) -> None:
        with self._lock:
            self._templates.clear()
            self._prepared.clear()
            self._last_fired.clear()

    def reload_templates(self) -> None:
        """Drop cached decoded images; files are re-read on the next scan."""
        with self._lock:
            self._prepared.clear()

    def reset_cooldowns(self) -> None:
        with self._lock:
            self._last_fired.clear()

    def start(self) -> bool:
        """Start the daemon scanner; return ``False`` if already running."""
        # Fail synchronously when the required numeric/matching backends are
        # missing.  This lets a UI show an actionable install message instead
        # of marking the feature as running while a worker repeatedly errors.
        _optional_backends()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._generation += 1
            generation = self._generation
            stop = threading.Event()
            self._stop = stop
            thread = threading.Thread(
                target=self._run,
                args=(stop, generation),
                name="vision-scan-worker",
                daemon=True,
            )
            self._thread = thread
            self.last_error = None
        try:
            thread.start()
        except Exception:
            with self._lock:
                if self._thread is thread and generation == self._generation:
                    self._thread = None
                    self._stop = None
            raise
        return True

    def stop(self, wait: bool = True, timeout: float = 1.5) -> None:
        """Request a stop and optionally wait for the scanner to exit."""
        with self._lock:
            stop = self._stop
            thread = self._thread
            if stop is not None:
                stop.set()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            if self._thread is thread and thread is not None and not thread.is_alive():
                self._thread = None
                self._stop = None

    def scan_once(self, *, trigger: bool = True) -> list[VisionMatch]:
        """Capture and match all enabled templates once.

        ``trigger=False`` always runs a full, read-only scan. Immediate mode
        dispatches the first hit and recaptures before looking for another.
        """
        scan_started = time.perf_counter()
        cv2, np = _optional_backends()
        self.last_error = None
        self.last_scan_details = []
        frame = self._capture()
        self.last_capture_ms = (time.perf_counter() - scan_started) * 1000
        image = _as_bgr(frame.image, cv2, np)
        excluded = tuple(self.exclude_regions() or ()) if self.exclude_regions else ()
        matches: list[VisionMatch] = []
        with self._lock:
            templates = list(self._templates)
        immediate = self.immediate_click and trigger
        ordered = list(enumerate(templates))
        if immediate and ordered:
            offset = self._next_template % len(ordered)
            ordered = ordered[offset:] + ordered[:offset]
        pending_scales = []

        def make_matches(selected, spec, search_x, search_y):
            now = time.time()
            return [VisionMatch(
                template_id=spec.key, template_name=spec.name or spec.key,
                score=score, x=search_x + px, y=search_y + py,
                width=tw, height=th, center_x=search_x + px + tw // 2,
                center_y=search_y + py + th // 2, timestamp=now,
                button=spec.button, threshold=spec.threshold,
                actions=((TemplateAction("click", spec.button, count=1, interval=0),)
                         if immediate else spec.action_plan),
            ) for score, px, py, tw, th in selected]

        def finish():
            self.last_scan_ms = (time.perf_counter() - scan_started) * 1000
            if trigger:
                self._trigger(matches, templates)
            return matches

        for index, spec in ordered:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            if not spec.enabled:
                continue
            try:
                prepared = self._get_prepared(spec, index, cv2, np)
                search, search_x, search_y = _crop_absolute(
                    image, frame.origin_x, frame.origin_y,
                    spec.region or self.region, np,
                )
                detail = {"name": spec.name or spec.key, "score": 0.0,
                          "scale": 1.0, "threshold": spec.threshold,
                          "frame_size": (int(image.shape[1]), int(image.shape[0]))}
                self.last_scan_details.append(detail)
                if search.size == 0:
                    detail["reason"] = "识别区域在截图范围之外"
                    continue
                source = search
                template_image = prepared.image
                if len(source.shape) == 2 and len(template_image.shape) == 3:
                    source = cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
                elif len(source.shape) == 3 and len(template_image.shape) == 2:
                    template_image = cv2.cvtColor(template_image, cv2.COLOR_GRAY2BGR)
                if spec.grayscale:
                    source = self._to_gray(source, cv2)
                    template_image = self._to_gray(template_image, cv2)
                threshold = _validate_threshold(spec.threshold, self.threshold)
                selected = self._match_scaled(
                    source, template_image, prepared, threshold,
                    search_x, search_y, excluded, detail, cv2, np,
                    quick_only=immediate, maximum=1 if immediate else None,
                )
                matches.extend(make_matches(selected, spec, search_x, search_y))
                if immediate:
                    if matches:
                        # Click before any absent/slow template is searched,
                        # then recapture. Rotate priority to avoid starvation.
                        self._next_template = index + 1
                        return finish()
                    pending_scales.append((index, spec, source, template_image,
                                           prepared, threshold, search_x, search_y, detail))
            except Exception as exc:
                self._report_error(exc)
        if immediate and pending_scales:
            # Only one template gets a bounded scale-search slice per frame.
            # Every other target gets another quick check on the next capture.
            pending_scales.sort(key=lambda item: item[0])
            index, spec, source, template_image, prepared, threshold, sx, sy, detail = (
                pending_scales[self._scale_template % len(pending_scales)])
            self._scale_template += 1
            steps = [50, 30, 60, 20, 80] + [step for step in range(20, 81)
                                           if step not in {50, 30, 60, 20, 80}]
            cursor = prepared.scale_cursor
            batch = [steps[(cursor + i) % len(steps)] for i in range(6)]
            prepared.scale_cursor = (cursor + len(batch)) % len(steps)
            try:
                selected = self._match_scaled(
                    source, template_image, prepared, threshold, sx, sy,
                    excluded, detail, cv2, np, scale_steps=batch,
                    skip_quick=True, maximum=1,
                )
                matches.extend(make_matches(selected, spec, sx, sy))
                if matches:
                    self._next_template = index + 1
            except Exception as exc:
                self._report_error(exc)
        return finish()

    @staticmethod
    def _score_map(source, template, cv2, np):
        variance = float(np.max(np.std(template, axis=(0, 1))))
        if variance < 1e-6:
            # Normalized correlation is undefined for constant templates.
            return 1.0 - cv2.matchTemplate(source, template, cv2.TM_SQDIFF_NORMED)
        return cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)

    @staticmethod
    def _exclude_scores(scores, width, height, origin_x, origin_y, excluded):
        for left, top, region_width, region_height in excluded:
            # Exclude all candidate rectangles touching our UI before taking
            # the best N; otherwise a preview can consume the only match slot.
            x0 = max(0, left - origin_x - width + 1)
            y0 = max(0, top - origin_y - height + 1)
            x1 = min(scores.shape[1], left + region_width - origin_x)
            y1 = min(scores.shape[0], top + region_height - origin_y)
            if x1 > x0 and y1 > y0:
                scores[y0:y1, x0:x1] = -1.0

    def _match_scaled(self, source, template, prepared, threshold,
                      origin_x, origin_y, excluded, detail, cv2, np, *,
                      quick_only=False, scale_steps=None, skip_quick=False,
                      maximum=None):
        selected = []
        attempted = set()
        maximum = maximum or max(1, int(prepared.spec.max_matches))
        sh, sw = source.shape[:2]
        stop = self.stop_event

        def attempt(scale):
            if stop is not None and stop.is_set():
                return
            tw = max(2, round(prepared.width * scale))
            th = max(2, round(prepared.height * scale))
            if (tw, th) in attempted or tw > sw or th > sh:
                return
            attempted.add((tw, th))
            resized = template if (tw, th) == (prepared.width, prepared.height) else cv2.resize(
                template, (tw, th), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            scores = self._score_map(source, resized, cv2, np)
            self._exclude_scores(scores, tw, th, origin_x, origin_y, excluded)
            scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
            best = cv2.minMaxLoc(scores)[1]
            if best > detail["score"]:
                detail.update(score=float(best), scale=float(scale))
            # Greedy peaks avoid allocating millions of threshold locations
            # on large desktops or on nearly uniform templates.
            for _ in range(maximum * 4):
                _, score, _, (px, py) = cv2.minMaxLoc(scores)
                if score < threshold:
                    break
                rect = (px, py, tw, th)
                if not any(_rect_iou(rect, (sx, sy, w, h)) > 0.30
                           for _, sx, sy, w, h in selected):
                    selected.append((float(score), px, py, tw, th))
                    prepared.preferred_scale = scale
                scores[max(0, py-th//2):py+th//2+1,
                       max(0, px-tw//2):px+tw//2+1] = -1.0
                if len(selected) >= maximum:
                    break

        if not skip_quick:
            attempt(prepared.preferred_scale)
            if len(selected) < maximum:
                attempt(1.0)
        if quick_only or len(selected) >= maximum:
            return selected

        # Search 50%-200% in a small image, then verify only the three best
        # scales against the original pixels and the unchanged threshold.
        # This bounds expensive full-resolution matching on 4K monitors.
        ratio = min(1.0, 800.0 / max(sw, sh))
        small_source = self._to_gray(source, cv2)
        if ratio < 1:
            small_source = cv2.resize(small_source, (round(sw*ratio), round(sh*ratio)),
                                      interpolation=cv2.INTER_AREA)
        gray_template = self._to_gray(template, cv2)
        ranking = []
        sizes = set()
        for step in (scale_steps if scale_steps is not None else range(20, 81)):
            if stop is not None and stop.is_set():
                break
            scale = step / 40.0
            tw, th = round(prepared.width*scale), round(prepared.height*scale)
            w, h = max(2, round(tw*ratio)), max(2, round(th*ratio))
            if (w, h) in sizes or tw > sw or th > sh or w > small_source.shape[1] or h > small_source.shape[0]:
                continue
            sizes.add((w, h))
            small_template = cv2.resize(gray_template, (w, h),
                                        interpolation=cv2.INTER_AREA if scale*ratio < 1 else cv2.INTER_LINEAR)
            scores = self._score_map(small_source, small_template, cv2, np)
            small_excluded = [(round((x-origin_x)*ratio), round((y-origin_y)*ratio),
                               math.ceil(width*ratio), math.ceil(height*ratio))
                              for x, y, width, height in excluded]
            self._exclude_scores(scores, w, h, 0, 0, small_excluded)
            ranking.append((cv2.minMaxLoc(scores)[1], scale))
        for rough_score, scale in sorted(ranking, reverse=True)[:3]:
            if scale_steps is not None and rough_score < threshold * 0.75:
                continue
            attempt(scale)
            if len(selected) >= maximum:
                break
        if not attempted and (prepared.width * .5 > sw or prepared.height * .5 > sh):
            detail["reason"] = "模板比截图大，请检查目标窗口大小"
        elif float(np.max(np.std(source, axis=(0, 1)))) < 1:
            detail["reason"] = "截图为空白或纯色，请确认窗口未最小化且内容已显示"
        return sorted(selected, reverse=True)[:maximum]

    def scan_summary(self) -> str:
        """A useful no-match diagnostic without changing matching thresholds."""
        if not self.last_scan_details:
            return "没有可扫描的目标"
        best = max(self.last_scan_details, key=lambda item: item["score"])
        if best.get("reason"):
            return best["reason"]
        width, height = best["frame_size"]
        return (f"最高匹配度 {best['score']:.0%} / 阈值 {best['threshold']:.0%}"
                f" · 缩放 {best['scale']:.0%} · 截图 {width}×{height}"
                f" · 本轮检测 {self.last_scan_ms:.0f} ms")

    def _capture(self) -> ScreenFrame:
        if self.capture_fn is None:
            return self.capturer.capture(self.region)
        try:
            raw = self.capture_fn(self.region)
        except TypeError:
            # Convenience for a zero-argument test/provider callback.
            raw = self.capture_fn()  # type: ignore[misc]
        if isinstance(raw, ScreenFrame):
            return raw
        # A raw image returned by a custom provider is assumed to represent
        # the requested region when one was configured.  Providers that
        # capture a larger frame can return ScreenFrame (or an explicit
        # origin tuple) to override this assumption.
        origin_x, origin_y = ((self.region[0], self.region[1])
                              if self.region is not None else (0, 0))
        image = raw
        if isinstance(raw, tuple) and len(raw) in (2, 3):
            image = raw[0]
            if len(raw) == 3:
                origin_x, origin_y = int(raw[1]), int(raw[2])
            elif isinstance(raw[1], (tuple, list)) and len(raw[1]) == 2:
                origin_x, origin_y = int(raw[1][0]), int(raw[1][1])
        return ScreenFrame(image=image, origin_x=origin_x, origin_y=origin_y,
                           timestamp=time.time())

    @staticmethod
    def _to_gray(image: Any, cv2: Any) -> Any:
        if len(image.shape) == 2:
            return image
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    def _get_prepared(self, spec: TemplateSpec, index: int,
                      cv2: Any, np: Any) -> _PreparedTemplate:
        key = id(spec)
        with self._lock:
            prepared = self._prepared.get(key)
        if prepared is not None:
            return prepared
        source = spec.image if spec.image is not None else spec.path
        image = _read_image(source, cv2, np)
        if image is None:
            raise TemplateLoadError(
                f"模板 {spec.key!r} 没有可读取的 image/path"
            )
        if image.ndim == 2:
            height, width = image.shape[:2]
        else:
            height, width = image.shape[:2]
        if width <= 0 or height <= 0:
            raise TemplateLoadError(f"模板 {spec.key!r} 为空")
        prepared = _PreparedTemplate(spec=spec, image=image,
                                      width=int(width), height=int(height))
        with self._lock:
            self._prepared[key] = prepared
        return prepared

    def _trigger(self, matches: list[VisionMatch],
                 templates: list[TemplateSpec]) -> None:
        if not self.immediate_click:
            self._notify_scan(matches)
        spec_by_key = {spec.key: spec for spec in templates}
        for match in matches:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            spec = spec_by_key.get(match.template_id)
            if spec is None or (not self.immediate_click and not self._cooldown_ready(match, spec)):
                continue
            dispatch_started = time.perf_counter()
            if self.auto_click and spec.click:
                try:
                    if self.click_fn:
                        self.click_fn(match)
                    else:
                        if (len(match.actions) == 1
                                and match.actions[0].kind == "click"
                                and match.actions[0].count == 1):
                            self.click_at(match.center_x, match.center_y, match.button)
                        else:
                            self.click_at(match.center_x, match.center_y, match.button,
                                          actions=match.actions,
                                          stop_event=self.stop_event)
                except Exception as exc:
                    self._report_error(exc)
            if self.on_match:
                try:
                    self.on_match(match)
                except Exception as exc:
                    self._report_error(exc)
            self.last_dispatch_ms = (time.perf_counter() - dispatch_started) * 1000
        if self.immediate_click:
            self._notify_scan(matches)

    def _notify_scan(self, matches):
        if self.on_scan:
            try:
                self.on_scan(list(matches))
            except Exception as exc:
                self._report_error(exc)
        if self.on_cycle:
            try:
                self.on_cycle(list(matches))
            except Exception as exc:
                self._report_error(exc)

    def _cooldown_ready(self, match: VisionMatch, spec: TemplateSpec) -> bool:
        cooldown = max(0.0, float(spec.cooldown))
        if cooldown <= 0:
            return True
        now = time.time()
        key = spec.key
        radius = max(8, int(max(match.width, match.height) * 0.5))
        with self._lock:
            previous = self._last_fired.setdefault(key, [])
            previous[:] = [item for item in previous if now - item[2] < cooldown]
            for x, y, _timestamp in previous:
                if abs(x - match.center_x) <= radius and abs(y - match.center_y) <= radius:
                    return False
            previous.append((match.center_x, match.center_y, now))
        return True

    def _report_error(self, exc: BaseException) -> None:
        with self._lock:
            self.last_error = exc
        if self.on_error:
            try:
                self.on_error(exc)
            except Exception:
                pass

    def _run(self, stop: threading.Event, generation: int) -> None:
        next_tick = time.perf_counter()
        try:
            while not stop.is_set():
                try:
                    self.scan_once(trigger=True)
                except Exception as exc:
                    self._report_error(exc)
                if self.immediate_click:
                    # Yield to other threads; do not add the configured scan
                    # interval after screenshot/matching work in this mode.
                    stop.wait(0.001)
                    continue
                next_tick += self.interval
                # A screen capture/template match can take longer than the
                # requested interval (especially on a 4K desktop or when
                # several templates are enabled).  Continuing to add the
                # interval to an already stale deadline would make the worker
                # spin through a long series of zero-length waits, consuming a
                # CPU core and starving the UI.  Drop missed ticks and schedule
                # the next scan one interval after the current cycle instead.
                now = time.perf_counter()
                if next_tick <= now:
                    next_tick = now + self.interval
                stop.wait(next_tick - now)
        finally:
            with self._lock:
                current = (self._thread is threading.current_thread()
                           and generation == self._generation)
                if current:
                    self._thread = None
                    self._stop = None
            if current and self.on_done:
                try:
                    self.on_done()
                except Exception as exc:
                    self._report_error(exc)

    @staticmethod
    def click_at(x: int, y: int, button: str = "left",
                 actions: Any = None,
                 stop_event: Optional[threading.Event] = None) -> None:
        """Move the Windows cursor and execute a template action plan.

        ``actions`` is optional for compatibility; omitted calls perform one
        click using ``button`` exactly as older integrations expect.
        """
        if platform.system() != "Windows":
            raise OSError("automatic clicking is only supported on Windows")
        try:
            user32 = ctypes.windll.user32
            if not user32.SetCursorPos(int(x), int(y)):
                raise ctypes.WinError()
            # Import lazily so a preview-only use does not depend on core.py.
            plan = actions if actions else (TemplateAction("click", button),)
            execute_template_actions(plan, stop_event=stop_event)
        except ImportError as exc:
            raise VisionDependencyError("无法加载点击模块 core.py") from exc


class ScreenMatcher(VisionEngine):
    """Backward-compatible name used by the Clicker Pro UI.

    The UI historically called the polling option ``scan_interval`` and
    performs the actual click in its ``on_match`` callback.  Consequently
    this wrapper keeps automatic clicking disabled unless the caller
    explicitly passes ``auto_click=True``.
    """

    def __init__(self, templates: Optional[Iterable[TemplateSpec]] = None,
                 scan_interval: float = 0.12, **kwargs: Any):
        kwargs.setdefault("interval", scan_interval)
        super().__init__(templates, **kwargs)


# Names retained for integrations that predate the more descriptive classes.
MatchResult = VisionMatch


__all__ = [
    "CaptureError",
    "Region",
    "ScreenCapturer",
    "ScreenFrame",
    "TemplateLoadError",
    "TemplateAction",
    "TemplateSpec",
    "VisionDependencyError",
    "VisionEngine",
    "VisionError",
    "VisionMatch",
    "execute_template_actions",
    "normalise_actions",
    "MatchResult",
    "ScreenMatcher",
]
