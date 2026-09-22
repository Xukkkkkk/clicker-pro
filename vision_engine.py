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
from collections import OrderedDict
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
                        # mss returns BGRA.  Wrapping its buffer and letting
                        # OpenCV drop the alpha channel is several times
                        # faster than slicing a numpy view and copying it:
                        # on a 5120x1440 desktop that is ~3 ms instead of
                        # ~26 ms per frame.
                        try:
                            image = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                                shot.height, shot.width, 4)
                            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
                        except (AttributeError, ValueError, TypeError):
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


# Longest edge of the reduced image used to propose candidate locations.
# Proposals only have to be roughly right: every candidate is re-checked with
# original pixels afterwards.  Halving this edge quarters the cost of the
# scale sweep, which dominates every scan that finds nothing.
COARSE_SOURCE_EDGE = 640.0
# One proposal search per ~7% size change instead of one per 2.5% step.  All
# native sizes inside a winning bucket are still verified individually, so a
# scale is never skipped -- only the coarse search for it is shared.
COARSE_SCALE_RATIO = 1.07


def _scale_bucket_key(scale: float) -> int:
    return int(round(math.log(scale) / math.log(COARSE_SCALE_RATIO)))


def _build_scale_ladder() -> tuple[dict[int, tuple[float, ...]], tuple[int, ...]]:
    """Group the 50%-200% native scales into coarse proposal buckets.

    The returned order checks the sizes produced by the usual Windows display
    scaling factors first, so a rescaled UI is normally found by the very
    first bucket that is searched.
    """
    buckets: dict[int, list[float]] = {}
    for step in range(20, 81):
        scale = step / 40.0
        buckets.setdefault(_scale_bucket_key(scale), []).append(scale)
    order: list[int] = []
    for step in (50, 30, 60, 20, 80):
        key = _scale_bucket_key(step / 40.0)
        if key not in order:
            order.append(key)
    order.extend(key for key in sorted(buckets) if key not in order)
    return ({key: tuple(values) for key, values in buckets.items()}, tuple(order))


SCALE_BUCKETS, SCALE_BUCKET_ORDER = _build_scale_ladder()


def _coarse_ratio(source_width: int, source_height: int,
                  template_width: int, template_height: int) -> float:
    """Downscale factor used for candidate proposals, never below 12 px."""
    return min(1.0, max(COARSE_SOURCE_EDGE / max(1, max(source_width, source_height)),
                        12.0 / max(1, min(template_width, template_height))))


@dataclass
class _PreparedTemplate:
    spec: TemplateSpec
    image: Any
    width: int
    height: int
    preferred_scale: float = 1.0
    # Width and height round independently, so some on-screen sizes are not
    # reachable from any single scale factor.  Keeping the size that actually
    # matched lets the next scan re-check it directly.
    preferred_size: Optional[Tuple[int, int]] = None
    scale_cursor: int = 0
    last_rect: Optional[Region] = None
    sweep_at: float = 0.0
    # Derived values that only depend on the template pixels.  Recomputing
    # them per scan cost a few milliseconds on every cycle.
    gray: Any = field(default=None, repr=False)
    gray_ndim: int = 0
    gray_variance: float = -1.0
    color_variance: float = -1.0


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
        adaptive_region: bool = True,
        region_refresh: float = 0.5,
        sweep_interval: float = 0.4,
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
        # Grabbing the whole virtual desktop is by far the slowest part of a
        # scan on a large or multi-monitor setup.  Once every enabled target
        # has a known position, capture just the area around them and take a
        # full frame again at least every ``region_refresh`` seconds so a
        # target that moved or appeared elsewhere is still found quickly.
        self.adaptive_region = bool(adaptive_region)
        self.region_refresh = max(0.0, float(region_refresh))
        # Searching every size from 50% to 200% is only needed until a target
        # has been seen once.  While scanning, repeat that search at most
        # every ``sweep_interval`` seconds and spend the frames in between on
        # the size that already matched, so a target that comes back is acted
        # on in milliseconds instead of after a full search.  A read-only
        # ``scan_once(trigger=False)`` always searches everything.
        self.sweep_interval = max(0.0, float(sweep_interval))
        self._full_frame_rect: Optional[Region] = None
        self._full_frame_at = 0.0
        self.last_scan_details: list[dict[str, Any]] = []
        self.last_scan_ms = 0.0
        self.last_capture_ms = 0.0
        self.last_dispatch_ms = 0.0
        self._next_template = 0
        self._scale_template = 0
        self._scan_images: dict = {}
        self._image_cache: OrderedDict = OrderedDict()
        self._image_cache_bytes = 0
        self._image_cache_limit = 32 * 1024 * 1024

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
                    self._image_cache.clear()
                    self._image_cache_bytes = 0
                    self._last_fired.pop(key, None)
                    return True
        return False

    def clear_templates(self) -> None:
        with self._lock:
            self._templates.clear()
            self._prepared.clear()
            self._image_cache.clear()
            self._image_cache_bytes = 0
            self._last_fired.clear()

    def reload_templates(self) -> None:
        """Drop cached decoded images; files are re-read on the next scan."""
        with self._lock:
            self._prepared.clear()
            self._image_cache.clear()
            self._image_cache_bytes = 0

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

        ``trigger=False`` always runs a full, read-only scan. Matches are
        dispatched as soon as their template is searched. After a dispatch,
        subsequent templates use a fresh capture because an action can change
        the screen. Immediate mode returns after its first hit.
        """
        scan_started = time.perf_counter()
        cv2, np = _optional_backends()
        self.last_error = None
        self.last_scan_details = []
        self._scan_images = {}
        with self._lock:
            templates = list(self._templates)
        capture_region = self._plan_capture_region(templates)
        frame = self._capture(capture_region)
        self.last_capture_ms = (time.perf_counter() - scan_started) * 1000
        image = _as_bgr(frame.image, cv2, np)
        excluded = tuple(self.exclude_regions() or ()) if self.exclude_regions else ()
        matches: list[VisionMatch] = []
        immediate = self.immediate_click and trigger
        ordered = list(enumerate(templates))
        if immediate and ordered:
            offset = self._next_template % len(ordered)
            ordered = ordered[offset:] + ordered[:offset]
        pending_scales = []
        dispatch_seconds = 0.0
        refresh_frame = False

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
            self.last_scan_ms = (time.perf_counter() - scan_started - dispatch_seconds) * 1000
            if trigger:
                if immediate:
                    self._trigger(matches, templates)
                else:
                    self._notify_scan(matches)
            return matches

        for index, spec in ordered:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            if not spec.enabled:
                continue
            try:
                if refresh_frame:
                    capture_started = time.perf_counter()
                    frame = self._capture(capture_region)
                    image = _as_bgr(frame.image, cv2, np)
                    self.last_capture_ms += (time.perf_counter() - capture_started) * 1000
                    excluded = tuple(self.exclude_regions() or ()) if self.exclude_regions else ()
                    self._scan_images = {}
                    refresh_frame = False
                    if self.stop_event is not None and self.stop_event.is_set():
                        break
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
                quick_only = immediate
                if trigger and not immediate:
                    now = time.monotonic()
                    quick_only = now - prepared.sweep_at < self.sweep_interval
                    if not quick_only:
                        prepared.sweep_at = now
                selected = self._match_scaled(
                    source, template_image, prepared, threshold,
                    search_x, search_y, excluded, detail, cv2, np,
                    quick_only=quick_only, maximum=1 if immediate else None,
                )
                current_matches = make_matches(selected, spec, search_x, search_y)
                matches.extend(current_matches)
                if immediate:
                    if matches:
                        # Click before any absent/slow template is searched,
                        # then recapture. Rotate priority to avoid starvation.
                        self._next_template = index + 1
                        return finish()
                    pending_scales.append((index, spec, source, template_image,
                                           prepared, threshold, search_x, search_y, detail))
                elif trigger and current_matches:
                    # Do not leave a known target waiting behind expensive
                    # searches for other templates. Cooldowns remain enforced
                    # by _trigger; only an actual callback/input invalidates
                    # this frame for the following template.
                    dispatch_started = time.perf_counter()
                    self.last_scan_ms = (dispatch_started - scan_started - dispatch_seconds) * 1000
                    refresh_frame = self._trigger(current_matches, templates, notify=False)
                    dispatch_seconds += time.perf_counter() - dispatch_started
            except Exception as exc:
                self._report_error(exc)
        if immediate and pending_scales:
            # Only one template gets a bounded scale-search slice per frame.
            # Every other target gets another quick check on the next capture.
            pending_scales.sort(key=lambda item: item[0])
            index, spec, source, template_image, prepared, threshold, sx, sy, detail = (
                pending_scales[self._scale_template % len(pending_scales)])
            self._scale_template += 1
            order = SCALE_BUCKET_ORDER
            cursor = prepared.scale_cursor
            batch = [order[(cursor + i) % len(order)] for i in range(4)]
            prepared.scale_cursor = (cursor + len(batch)) % len(order)
            try:
                selected = self._match_scaled(
                    source, template_image, prepared, threshold, sx, sy,
                    excluded, detail, cv2, np, bucket_keys=batch,
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
        variance = float(cv2.meanStdDev(template)[1].max())
        if variance < 1e-6:
            # SQDIFF_NORMED divides by zero for black templates. RMS pixel
            # error has a meaningful score for every constant colour.
            errors = cv2.matchTemplate(source, template, cv2.TM_SQDIFF)
            np.maximum(errors, 0, out=errors)
            errors /= float(template.size * 255.0 ** 2)
            np.sqrt(errors, out=errors)
            return 1.0 - errors
        return cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)

    def _cached_resize(self, prepared, image, width, height, kind, cv2):
        key = (id(prepared.spec), kind, width, height)
        cached = self._image_cache.get(key)
        if cached is not None:
            self._image_cache.move_to_end(key)
            return cached
        if (width, height) == (image.shape[1], image.shape[0]):
            return image
        resized = cv2.resize(image, (width, height), interpolation=(
            cv2.INTER_AREA if width < image.shape[1] else cv2.INTER_LINEAR))
        size = resized.nbytes
        if size <= self._image_cache_limit:
            while self._image_cache and (self._image_cache_bytes + size > self._image_cache_limit
                                         or len(self._image_cache) >= 512):
                _, evicted = self._image_cache.popitem(last=False)
                self._image_cache_bytes -= evicted.nbytes
            self._image_cache[key] = resized
            self._image_cache_bytes += size
        return resized

    def _search_image(self, source, origin_x, origin_y, ratio, cv2, *, color=False):
        region_key = (origin_x, origin_y, source.shape[:2], "color" if color else "gray")
        key = (*region_key, ratio)
        cached = self._scan_images.get(key)
        if cached is not None:
            return cached
        gray_key = (*region_key, 1.0)
        gray = self._scan_images.get(gray_key)
        if gray is None:
            gray = source if color else self._to_gray(source, cv2)
        result = gray if ratio == 1 else cv2.resize(
            gray, (max(1, round(source.shape[1]*ratio)), max(1, round(source.shape[0]*ratio))),
            interpolation=cv2.INTER_AREA)
        # Region-specific templates must not retain dozens of desktop copies.
        if len(self._scan_images) >= 8:
            self._scan_images.clear()
        self._scan_images[gray_key] = gray
        self._scan_images[key] = result
        return result

    @staticmethod
    def _peak_locations(scores, width, height, threshold, limit, cv2, *, minimum=0):
        """Best candidate locations in a proposal score map.

        ``minimum`` keeps that many peaks even when they score below
        ``threshold``.  A downscaled proposal of a thin or anti-aliased target
        can score poorly while the original pixels still match exactly, so the
        sweep asks for one peak per size regardless and lets the native-pixel
        verification make the decision.
        """
        peaks = []
        for _ in range(limit):
            _, score, _, position = cv2.minMaxLoc(scores)
            if not math.isfinite(score) or (score < threshold and len(peaks) >= minimum):
                break
            x, y = position
            peaks.append((float(score), x, y))
            scores[max(0, y-height//2):y+height//2+1,
                   max(0, x-width//2):x+width//2+1] = -1
        return peaks

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
                      quick_only=False, bucket_keys=None, skip_quick=False,
                      maximum=None):
        selected = []
        attempted = set()
        maximum = maximum or max(1, int(prepared.spec.max_matches))
        sh, sw = source.shape[:2]
        stop = self.stop_event
        if (prepared.gray is None or prepared.gray_ndim != template.ndim
                or prepared.gray.shape[:2] != template.shape[:2]):
            prepared.gray_ndim = int(template.ndim)
            prepared.gray = self._to_gray(template, cv2)
            prepared.gray_variance = float(cv2.meanStdDev(prepared.gray)[1].max())
            prepared.color_variance = (float(cv2.meanStdDev(template)[1].max())
                                       if template.ndim == 3 else prepared.gray_variance)
        gray_template = prepared.gray
        gray_variance = prepared.gray_variance
        color_proposals = (template.ndim == 3 and gray_variance < 1
                           and prepared.color_variance >= 1)
        # Downsampling at an odd screen position shifts a small icon by a
        # fraction of a pixel. Proposal scores may be low even for exact
        # native matches, so only original-pixel verification uses threshold.
        rough_threshold = .4 if min(prepared.width, prepared.height) <= 32 else .5
        candidate_limit = max(12, maximum * 4)
        # The best few near misses of this scan, kept one per location so a
        # strong false peak elsewhere cannot hide the real target.
        near: list[tuple[float, Region]] = []

        def remember(score, px, py, tw, th):
            rect = (int(px), int(py), int(tw), int(th))
            for index, (previous, other) in enumerate(near):
                if _rect_iou(rect, other) > .30:
                    if score > previous:
                        near[index] = (float(score), rect)
                        near.sort(key=lambda item: item[0], reverse=True)
                    return
            near.append((float(score), rect))
            near.sort(key=lambda item: item[0], reverse=True)
            del near[3:]

        def verify(resized, scale, x0=0, y0=0, x1=None, y1=None):
            tw, th = resized.shape[1], resized.shape[0]
            x1, y1 = sw if x1 is None else x1, sh if y1 is None else y1
            if x1-x0 < tw or y1-y0 < th:
                return
            scores = self._score_map(source[y0:y1, x0:x1], resized, cv2, np)
            self._exclude_scores(scores, tw, th, origin_x+x0, origin_y+y0, excluded)
            np.nan_to_num(scores, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
            for _ in range(candidate_limit):
                _, correlation, _, (local_x, local_y) = cv2.minMaxLoc(scores)
                if correlation < threshold:
                    if correlation > detail["score"]:
                        detail.update(score=float(correlation), scale=float(scale))
                    remember(correlation, local_x+x0, local_y+y0, tw, th)
                    break
                px, py = local_x+x0, local_y+y0
                patch = source[py:py+th, px:px+tw]
                # Correlation ignores mean colour/brightness. Verify actual
                # pixels too so a similarly shaped, differently coloured
                # control cannot masquerade as an exact match.
                pixel_score = max(0.0, 1.0 - cv2.norm(patch, resized, cv2.NORM_L2)
                                  / (math.sqrt(resized.size) * 255.0))
                score = min(float(correlation), pixel_score)
                if score > detail["score"]:
                    detail.update(score=score, scale=float(scale))
                remember(score, px, py, tw, th)
                rect = (px, py, tw, th)
                if score >= threshold and not any(
                        _rect_iou(rect, (sx, sy, w, h)) > .30 for _, sx, sy, w, h in selected):
                    selected.append((score, px, py, tw, th))
                    prepared.preferred_scale = scale
                    prepared.preferred_size = (tw, th)
                    prepared.last_rect = (origin_x+px, origin_y+py, tw, th)
                scores[max(0, local_y-th//2):local_y+th//2+1,
                       max(0, local_x-tw//2):local_x+tw//2+1] = -1
                if len(selected) >= maximum:
                    break

        def sized(scale, size=None):
            """Resize the template; ``None`` when that size was already tried."""
            if size is None:
                tw = max(2, round(prepared.width * scale))
                th = max(2, round(prepared.height * scale))
            else:
                tw, th = max(2, int(size[0])), max(2, int(size[1]))
            if (tw, th) in attempted or tw > sw or th > sh:
                return None, tw, th
            attempted.add((tw, th))
            return self._cached_resize(prepared, template, tw, th,
                                       "gray" if template.ndim == 2 else "color", cv2), tw, th

        def refine():
            """Retry the best near miss at pixel-accurate template sizes.

            The searched scales step by 2.5%, and width and height round
            independently, so a target whose real size falls between two
            steps stays a pixel or two off however finely the scales are
            spaced.  That is enough to push an otherwise exact match below a
            strict threshold.  Walking one pixel at a time around the best
            candidates costs a handful of windowed checks, and the size that
            succeeds is kept in ``preferred_size`` so later scans skip the
            search entirely.
            """
            aspect = prepared.height / prepared.width
            # Changing the size by a few pixels barely moves the best
            # top-left corner, so a tight window keeps each retry cheap.
            margin = 8
            # An exploratory batch only searches a few size bands and runs on
            # every frame, so it refines the single best candidate; a full
            # sweep can afford to retry the best few locations.
            for score, (bx, by, bw, bh) in list(near)[:1 if bucket_keys is not None else 3]:
                if score < .5:
                    break
                for dx in (0, 1, -1, 2, -2, 3, -3):
                    for dy in (0, 1, -1):
                        if selected or (stop is not None and stop.is_set()):
                            return
                        width = bw + dx
                        height = round(width * aspect) + dy
                        scale = width / prepared.width
                        if not (.5 <= scale <= 2.0 and height >= 2):
                            continue
                        resized, tw, th = sized(scale, (width, height))
                        if resized is None:
                            continue
                        verify(resized, scale,
                               max(0, bx-margin), max(0, by-margin),
                               min(sw, bx+tw+margin), min(sh, by+th+margin))

        def attempt(scale, proposals=None, ratio=1.0, size=None):
            if stop is not None and stop.is_set():
                return
            resized, tw, th = sized(scale, size)
            if resized is None:
                return
            if proposals is None:
                # Revalidate the last location using fresh pixels, never
                # repeat a cached click. A moved/disappeared target falls
                # through to a full-frame search in the same scan.
                previous = prepared.last_rect
                if maximum == 1 and previous and previous[2:] == (tw, th):
                    px, py = previous[0]-origin_x, previous[1]-origin_y
                    margin = max(12, min(48, max(tw, th)//3))
                    verify(resized, scale, max(0, px-margin), max(0, py-margin),
                           min(sw, px+tw+margin), min(sh, py+th+margin))
                    if selected:
                        return
                    prepared.last_rect = None
                ratio = _coarse_ratio(sw, sh, tw, th)
                # Very small or isoluminant templates retain their full
                # colour search; reducing them can erase all useful detail.
                if ratio == 1 or gray_variance < 1:
                    verify(resized, scale)
                    return
                small_source = self._search_image(source, origin_x, origin_y, ratio, cv2)
                w, h = max(2, round(tw*ratio)), max(2, round(th*ratio))
                small_template = self._cached_resize(prepared, gray_template, w, h, "coarse", cv2)
                scores = self._score_map(small_source, small_template, cv2, np)
                np.nan_to_num(scores, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
                small_excluded = [(math.floor((x-origin_x)*ratio), math.floor((y-origin_y)*ratio),
                                   math.ceil(width*ratio), math.ceil(height*ratio))
                                  for x, y, width, height in excluded]
                self._exclude_scores(scores, w, h, 0, 0, small_excluded)
                proposals = self._peak_locations(scores, w, h, rough_threshold,
                                                  candidate_limit, cv2, minimum=1)
            margin = max(3, math.ceil(2/ratio))
            for _, sx, sy in proposals:
                px, py = round(sx/ratio), round(sy/ratio)
                verify(resized, scale, max(0, px-margin), max(0, py-margin),
                       min(sw, px+tw+margin), min(sh, py+th+margin))
                if len(selected) >= maximum or (stop is not None and stop.is_set()):
                    break

        if not skip_quick:
            # Re-check the exact size that matched last time before anything
            # else; that is the common case once a target has been located.
            attempt(prepared.preferred_scale, size=prepared.preferred_size)
            if len(selected) < maximum:
                attempt(1.0)
        if quick_only or len(selected) >= maximum:
            return selected

        # Search 50%-200% in a small image, then verify the eight best coarse
        # size bands in native-pixel regions using the unchanged threshold.
        # One proposal search now covers a ~7% band of sizes instead of a
        # single 2.5% step, which is what makes a scan that finds nothing
        # cost milliseconds instead of a third of a second.  Every native
        # size inside a winning band is still verified separately: rounding
        # must not erase a scale.
        ratio = _coarse_ratio(sw, sh, prepared.width, prepared.height)
        small_source = self._search_image(source, origin_x, origin_y, ratio, cv2, color=color_proposals)
        proposal_template = template if color_proposals else gray_template
        ranking = []
        groups = {}
        for key in (bucket_keys if bucket_keys is not None else SCALE_BUCKET_ORDER):
            members = {}
            for scale in SCALE_BUCKETS.get(key, ()):
                tw, th = round(prepared.width*scale), round(prepared.height*scale)
                if tw > sw or th > sh:
                    continue
                members.setdefault((tw, th), scale)
            if not members:
                continue
            # Propose with the middle of the band so that no member is more
            # than half a band away from the size actually searched for.
            centre = min(max(COARSE_SCALE_RATIO ** key, min(members.values())),
                         max(members.values()))
            w = max(2, round(prepared.width*centre*ratio))
            h = max(2, round(prepared.height*centre*ratio))
            if w > small_source.shape[1] or h > small_source.shape[0]:
                continue
            groups.setdefault((w, h), {}).update(members)
        small_excluded = [(math.floor((x-origin_x)*ratio), math.floor((y-origin_y)*ratio),
                           math.ceil(width*ratio), math.ceil(height*ratio))
                          for x, y, width, height in excluded]
        for (w, h), members in groups.items():
            if stop is not None and stop.is_set():
                break
            small_template = self._cached_resize(prepared, proposal_template, w, h,
                                                 "coarse-color" if color_proposals else "coarse", cv2)
            scores = self._score_map(small_source, small_template, cv2, np)
            np.nan_to_num(scores, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
            self._exclude_scores(scores, w, h, 0, 0, small_excluded)
            # Keep one peak per size even below the proposal threshold: a
            # downscaled thin or anti-aliased target can score poorly while
            # its original pixels still match exactly.  Ranking puts those
            # last, so they only cost a windowed check when nothing else
            # scored better.
            peaks = self._peak_locations(scores, w, h, rough_threshold,
                                         candidate_limit, cv2, minimum=1)
            if peaks:
                ranking.append((peaks[0][0], sorted(members.values()), peaks))
        for _, scales, proposals in sorted(ranking, key=lambda item: item[0], reverse=True)[:8]:
            for scale in scales:
                attempt(scale, proposals, ratio)
                if len(selected) >= maximum:
                    break
            if len(selected) >= maximum:
                break
        # A near miss is far more likely to be the target at a size between
        # two searched steps than a different control that happens to be
        # similar, so spend a few windowed checks before giving up.
        if not selected and near:
            refine()
        if not selected:
            if not attempted and (prepared.width * .5 > sw or prepared.height * .5 > sh):
                detail["reason"] = "模板比截图大，请检查目标窗口大小"
            else:
                # Inspect the reduced copy the sweep already built.  A
                # full-resolution statistics pass over a 4K frame cost
                # milliseconds on every scan, including successful ones.
                gray_source = self._search_image(source, origin_x, origin_y, ratio, cv2)
                minimum, maximum_value, _, _ = cv2.minMaxLoc(gray_source)
                if maximum_value - minimum < 1:
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
                f" · 截屏 {self.last_capture_ms:.0f} ms"
                f" · 本轮检测 {self.last_scan_ms:.0f} ms")

    def _capture(self, region: Optional[Region] = None) -> ScreenFrame:
        if self.capture_fn is None:
            if region is None:
                frame = self.capturer.capture(self.region)
                if self.region is None:
                    self._full_frame_rect = (
                        int(frame.origin_x), int(frame.origin_y),
                        int(frame.image.shape[1]), int(frame.image.shape[0]),
                    )
                    self._full_frame_at = time.monotonic()
                return frame
            return self.capturer.capture(region)
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

    def _plan_capture_region(self, templates: Sequence[TemplateSpec]) -> Optional[Region]:
        """Return a reduced capture rectangle, or ``None`` for a full frame.

        A custom ``capture_fn`` (background window capture) and an explicit
        ``region`` already limit what is grabbed, so both keep their own
        behavior.  Targets that may appear more than once, or that carry a
        region of their own, always get the full frame: a second occurrence
        can show up anywhere.
        """
        if (not self.adaptive_region or self.capture_fn is not None
                or self.region is not None):
            return None
        bounds = self._full_frame_rect
        if bounds is None or time.monotonic() - self._full_frame_at >= self.region_refresh:
            return None
        rects: list[Region] = []
        with self._lock:
            for spec in templates:
                if not spec.enabled:
                    continue
                if spec.region is not None or spec.max_matches > 1:
                    return None
                prepared = self._prepared.get(id(spec))
                rect = prepared.last_rect if prepared is not None else None
                if not rect:
                    return None
                rects.append(rect)
        if not rects:
            return None
        left = top = right = bottom = None
        for x, y, width, height in rects:
            # Keep enough room around each target that normal movement, and
            # any size between 50% and 200%, still lands inside the crop.
            pad = max(96, width, height)
            left = x - pad if left is None else min(left, x - pad)
            top = y - pad if top is None else min(top, y - pad)
            right = x + width + pad if right is None else max(right, x + width + pad)
            bottom = y + height + pad if bottom is None else max(bottom, y + height + pad)
        full_left, full_top, full_width, full_height = bounds
        left = max(full_left, left)
        top = max(full_top, top)
        right = min(full_left + full_width, right)
        bottom = min(full_top + full_height, bottom)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        # A crop that saves almost nothing is not worth the extra bookkeeping.
        if width * height > full_width * full_height * 0.6:
            return None
        return int(left), int(top), int(width), int(height)

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
                 templates: list[TemplateSpec], *, notify: bool = True) -> bool:
        """Dispatch eligible matches and report whether the frame may change."""
        dispatched = False
        spec_by_key = {spec.key: spec for spec in templates}
        for match in matches:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            spec = spec_by_key.get(match.template_id)
            if spec is None or (not self.immediate_click and not self._cooldown_ready(match, spec)):
                continue
            dispatch_started = time.perf_counter()
            if self.auto_click and spec.click:
                dispatched = True
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
                dispatched = True
                try:
                    self.on_match(match)
                except Exception as exc:
                    self._report_error(exc)
            self.last_dispatch_ms = (time.perf_counter() - dispatch_started) * 1000
        if notify:
            self._notify_scan(matches)
        return dispatched

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
