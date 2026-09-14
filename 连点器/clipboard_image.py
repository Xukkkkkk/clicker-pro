"""Windows clipboard image helpers used by the vision template editor.

Tk's :meth:`clipboard_get` API is text oriented and does not reliably expose
the Windows ``CF_DIB``/``CF_DIBV5`` formats used by screenshots.  Pillow's
``ImageGrab.grabclipboard`` handles those formats (and PNG clipboard data), so
this module keeps the platform specific detail in one small, lazy imported
place.  The helpers deliberately return detached Pillow images: the clipboard
owner may release its data as soon as the call returns.

The module has no import-time Pillow dependency.  That keeps the normal
clicker UI usable when optional image-recognition dependencies are absent and
also makes PyInstaller's dependency collection predictable (the application
spec explicitly includes ``PIL.ImageGrab``).
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"})


class ClipboardImageError(RuntimeError):
    """Raised when the clipboard cannot be queried or an image is invalid."""


@dataclass(frozen=True)
class ClipboardPayload:
    """A detached image or image files exposed by the clipboard.

    Windows Explorer places copied files on the clipboard as ``CF_HDROP``;
    Pillow exposes that as a list of paths.  Supporting that result is useful
    because users often copy a screenshot file and press Ctrl+V in the app.
    ``image`` and ``files`` are mutually exclusive in normal Pillow output.
    """

    image: Any = None
    files: tuple[Path, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.image is None and not self.files


def _detach_image(value: Any) -> Any:
    """Load and copy a Pillow image so it no longer depends on clipboard data."""

    # Import lazily; this module is imported by the UI even when vision is not
    # used and Pillow is an optional runtime dependency in source installs.
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise ClipboardImageError(
            "粘贴图片需要 Pillow，请先运行：python -m pip install Pillow"
        ) from exc

    if not isinstance(value, Image.Image):
        raise ClipboardImageError("剪贴板中的内容不是可识别的图片")
    try:
        # Check the header dimensions before loading pixels.  This prevents a
        # malformed/decompression-bomb clipboard payload from allocating a
        # huge buffer just to discover that it is not a usable template.
        width, height = value.size
        if width <= 0 or height <= 0:
            raise ClipboardImageError("剪贴板图片尺寸无效")
        # A very large or malformed clipboard image can otherwise allocate an
        # unreasonable amount of memory while being converted.  100 MP is
        # above the size of current desktop screenshots while still providing
        # a useful guard against decompression-bomb payloads.
        if int(width) * int(height) > 100_000_000:
            raise ClipboardImageError("剪贴板图片过大（最多支持 1 亿像素）")
        # ``DibImageFile`` and ``PngImageFile`` can retain a BytesIO/file-like
        # object owned by Pillow.  ``load`` followed by ``convert`` makes a
        # fully independent RGB image and also normalises palette/alpha modes
        # for OpenCV template matching.
        value.load()
        detached = value.convert("RGB")
        detached.load()
        return detached
    except ClipboardImageError:
        raise
    except Exception as exc:
        raise ClipboardImageError(f"无法读取剪贴板图片：{exc}") from exc


def _normalise_file_list(value: Any) -> tuple[Path, ...]:
    """Return existing image files from Pillow's CF_HDROP result."""

    if not isinstance(value, (list, tuple)):
        return ()
    result: list[Path] = []
    seen: set[str] = set()
    for raw in value:
        try:
            path = Path(os.fspath(raw)).expanduser()
        except (TypeError, ValueError, OSError):
            continue
        try:
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            absolute = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(str(absolute))
        if key in seen:
            continue
        seen.add(key)
        result.append(absolute)
    return tuple(result)


def read_clipboard(*, retries: int = 2, retry_delay: float = 0.04) -> ClipboardPayload:
    """Read an image or image-file list from the system clipboard.

    ``ImageGrab.grabclipboard`` may briefly fail while another application is
    rendering delayed clipboard data.  A couple of short retries make Ctrl+V
    from browsers and screenshot tools considerably more reliable without
    introducing a noticeable delay.  A missing/unsupported clipboard format is
    represented by an empty payload; API/decoding failures raise
    :class:`ClipboardImageError` with a user-facing message.
    """

    try:
        from PIL import ImageGrab
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise ClipboardImageError(
            "粘贴图片需要 Pillow，请先运行：python -m pip install Pillow"
        ) from exc

    attempts = max(1, int(retries) + 1)
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            value = ImageGrab.grabclipboard()
            if value is None:
                return ClipboardPayload()
            # Pillow returns Image.Image for CF_DIB/CF_DIBV5/PNG and a list of
            # paths for CF_HDROP.  Check the image branch first because image
            # objects can expose sequence-like attributes in some versions.
            try:
                from PIL import Image

                if isinstance(value, Image.Image):
                    return ClipboardPayload(image=_detach_image(value))
            except ImportError as exc:  # pragma: no cover
                raise ClipboardImageError(
                    "粘贴图片需要 Pillow，请先运行：python -m pip install Pillow"
                ) from exc
            files = _normalise_file_list(value)
            if files:
                return ClipboardPayload(files=files)
            # A non-empty, unsupported format is equivalent to no image.  It
            # is preferable to a cryptic type error in the UI.
            return ClipboardPayload()
        except ClipboardImageError:
            raise
        except (OSError, RuntimeError, NotImplementedError, ChildProcessError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(max(0.0, float(retry_delay)))
                continue
            raise ClipboardImageError(f"读取剪贴板失败：{exc}") from exc
        except Exception as exc:
            # Pillow's platform backend can surface ctypes/decoder exceptions
            # with different concrete types.  Retry once, then wrap them so
            # callers never need to know backend implementation details.
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(max(0.0, float(retry_delay)))
                continue
            raise ClipboardImageError(f"读取剪贴板失败：{exc}") from exc
    # The loop always returns or raises; keep a defensive error for unusual
    # monkey-patched backends used by integrations/tests.
    raise ClipboardImageError(f"读取剪贴板失败：{last_error or '未知错误'}")


def image_fingerprint(image: Any) -> str:
    """Return a stable SHA-256 fingerprint for a Pillow image.

    Fingerprints let the UI avoid adding the same pasted screenshot repeatedly
    even though every paste is written to a fresh filename.  The image is
    converted to RGB and loaded before hashing; this function does not mutate
    the caller's image.
    """

    detached = _detach_image(image)
    digest = hashlib.sha256()
    digest.update(f"{detached.width}x{detached.height}:RGB\0".encode("ascii"))
    digest.update(detached.tobytes())
    return digest.hexdigest()


def save_image(image: Any, directory: os.PathLike[str] | str, *, prefix: str = "pasted") -> Path:
    """Persist a clipboard image as a PNG and return its absolute path.

    The write is atomic (temporary file + ``os.replace``), so a process crash
    cannot leave a half-written template referenced by the saved config.
    ``directory`` is created if necessary; callers can pass their existing
    ``%LOCALAPPDATA%\\ClickerPro`` directory.
    """

    detached = _detach_image(image)
    target_dir = Path(directory).expanduser()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClipboardImageError(f"无法创建图片目录：{exc}") from exc

    safe_prefix = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(prefix))
    safe_prefix = safe_prefix.strip("._-") or "pasted"
    # UUID avoids collisions when several screenshots are pasted within one
    # clock tick and makes concurrent UI callbacks safe.
    target = target_dir / f"{safe_prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:10]}.png"
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        detached.save(temporary, format="PNG", optimize=False)
        os.replace(temporary, target)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ClipboardImageError(f"无法保存粘贴图片：{exc}") from exc
    return target.resolve()


def materialise_payload(
    payload: ClipboardPayload,
    directory: os.PathLike[str] | str,
    *,
    prefix: str = "pasted",
    copy_files: bool = False,
) -> tuple[Path, ...]:
    """Turn a clipboard payload into template paths.

    For an in-memory clipboard image this always writes a private PNG.  For
    copied image files, paths are returned directly by default (matching file
    picker behaviour); pass ``copy_files=True`` to copy them into ``directory``
    so the templates remain available if the originals are later moved.
    """

    if payload.image is not None:
        return (save_image(payload.image, directory, prefix=prefix),)
    if not payload.files:
        return ()
    if not copy_files:
        return payload.files

    # Copy through Pillow rather than shutil.copy2 so all supported formats
    # become self-contained PNG templates and malformed files are rejected.
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise ClipboardImageError(
            "粘贴图片需要 Pillow，请先运行：python -m pip install Pillow"
        ) from exc
    paths: list[Path] = []
    try:
        for index, source in enumerate(payload.files, 1):
            try:
                with Image.open(source) as opened:
                    opened.load()
                    detached = _detach_image(opened)
                paths.append(save_image(detached, directory, prefix=f"{prefix}_{index}"))
            except ClipboardImageError:
                raise
            except Exception as exc:
                raise ClipboardImageError(f"无法读取图片文件 {source}：{exc}") from exc
        return tuple(paths)
    except Exception:
        # A multi-file paste is one logical operation.  If a later source is
        # malformed or cannot be read, remove files already materialised for
        # earlier sources so a failed paste does not leave orphan templates in
        # the application's clipboard directory.  ``save_image`` always
        # returns newly-created paths here; ignore cleanup failures and retain
        # the original, actionable exception for the caller.
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except (OSError, RuntimeError, TypeError):
                pass
        raise


def paste_to_directory(
    directory: os.PathLike[str] | str,
    *,
    prefix: str = "pasted",
    copy_files: bool = False,
) -> tuple[Path, ...]:
    """Read the clipboard and return paths ready for a vision template list."""

    payload = read_clipboard()
    return materialise_payload(payload, directory, prefix=prefix, copy_files=copy_files)


__all__ = [
    "ClipboardImageError",
    "ClipboardPayload",
    "IMAGE_SUFFIXES",
    "image_fingerprint",
    "materialise_payload",
    "paste_to_directory",
    "read_clipboard",
    "save_image",
]
