from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
IMAGE_QUALITY_STEPS = (92, 86, 80, 74, 68, 62, 56)
IMAGE_PATH_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}
APPLESCRIPT_QUERY_TIMEOUT_SECONDS = 1.0
APPLESCRIPT_SET_CLIPBOARD_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ClipboardImage:
    mime_type: str
    data: bytes
    width: int
    height: int
    orientation: int
    signature: str


class ClipboardBackend(Protocol):
    def get_text(self) -> str:
        ...

    def set_text(self, text: str) -> None:
        ...

    def get_image(self) -> ClipboardImage | None:
        ...

    def set_image(self, image: ClipboardImage) -> None:
        ...


class MacClipboardBackend:
    """Mac clipboard backend supporting text + image payloads."""

    def get_text(self) -> str:
        import pyperclip

        value = pyperclip.paste()
        if value is None:
            return ""
        return str(value)

    def set_text(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text)

    def get_image(self) -> ClipboardImage | None:
        # Direct image data from browsers/media editors.
        try:
            from PIL import Image, ImageGrab

            content = ImageGrab.grabclipboard()
        except Exception:
            content = None
        direct_image = content if isinstance(content, Image.Image) else None

        # Finder and some apps place file references on the clipboard.
        if isinstance(content, list):
            for entry in content:
                path = Path(str(entry)).expanduser()
                image = _load_clipboard_image_from_path(path)
                if image is not None:
                    return image

        text_candidates = _clipboard_text_candidates(
            include_applescript_text=direct_image is None,
            include_file_url=direct_image is None,
        )
        for text_candidate in text_candidates:
            path = _clipboard_text_to_path(text_candidate)
            if path is not None:
                image = _load_clipboard_image_from_path(path)
                if image is not None:
                    return image

        if direct_image is None:
            finder_path = _finder_selected_path()
            if finder_path is not None and _finder_selection_matches_clipboard_candidates(
                finder_path,
                text_candidates,
            ):
                image = _load_clipboard_image_from_path(finder_path)
                if image is not None:
                    return image

        # Fall back to raw image clipboard data after exhausting file references.
        if direct_image is not None:
            return _prepare_clipboard_image(direct_image)

        return None

    def set_image(self, image: ClipboardImage) -> None:
        if _normalize_mime_type(image.mime_type) is None:
            raise ValueError(f"Unsupported image mime_type: {image.mime_type}")
        if len(image.data) == 0 or len(image.data) > MAX_IMAGE_BYTES:
            raise ValueError("Image payload is empty or too large")

        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(image.data)) as opened:
            exif_orientation = _extract_exif_orientation_degrees(opened)
            normalized = ImageOps.exif_transpose(opened)
            if image.orientation in {90, 180, 270} and image.orientation != exif_orientation:
                # Protocol orientation is clockwise, PIL rotate is counter-clockwise.
                # Avoid double-rotation if EXIF already encodes the same transform.
                normalized = normalized.rotate(-image.orientation, expand=True)
            output = io.BytesIO()
            normalized.save(output, format="PNG")
            _set_clipboard_png_bytes(output.getvalue())


@dataclass
class InMemoryClipboardBackend:
    """Clipboard backend for tests/simulation."""

    text: str = ""
    image: ClipboardImage | None = None

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def get_image(self) -> ClipboardImage | None:
        return self.image

    def set_image(self, image: ClipboardImage) -> None:
        self.image = image


class ClipboardPoller:
    """Polls clipboard and emits text/image updates."""

    def __init__(self, backend: ClipboardBackend, poll_interval: float = 0.4, logger=None):
        self.backend = backend
        self.poll_interval = poll_interval
        self.logger = logger
        self._last_text: str | None = None
        self._last_image_signature: str | None = None

    async def run(
        self,
        on_text_change: Callable[[str], Awaitable[None]],
        on_image_change: Callable[[ClipboardImage], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                text = self.backend.get_text()
                image = self.backend.get_image()
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(
                        "Clipboard read failed",
                        extra={"event": "clipboard.read.error"},
                        exc_info=exc,
                    )
                await asyncio.sleep(self.poll_interval)
                continue

            image_signature = image.signature if image is not None else None
            if self._last_text is None and self._last_image_signature is None:
                self._last_text = text
                self._last_image_signature = image_signature
                await asyncio.sleep(self.poll_interval)
                continue

            image_changed = image_signature != self._last_image_signature
            text_changed = text != self._last_text
            self._last_image_signature = image_signature

            # Prefer image events when both change in the same poll cycle.
            if image_changed and image is not None:
                self._last_text = text
                await on_image_change(image)
                await asyncio.sleep(self.poll_interval)
                continue

            if text_changed:
                self._last_text = text
                if text.strip():
                    await on_text_change(text)

            await asyncio.sleep(self.poll_interval)


def _prepare_clipboard_image(image: "Image.Image") -> ClipboardImage | None:
    from PIL import Image, ImageOps

    preferred_mime = _normalize_mime_type(Image.MIME.get(image.format))
    normalized = ImageOps.exif_transpose(image)
    encoded = _encode_image_for_protocol(normalized, preferred_mime=preferred_mime)
    if encoded is None:
        return None

    mime_type, data = encoded
    width, height = normalized.size
    return ClipboardImage(
        mime_type=mime_type,
        data=data,
        width=width,
        height=height,
        orientation=0,
        signature=_compute_image_signature(normalized),
    )


def _load_clipboard_image_from_path(path: Path) -> ClipboardImage | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        from PIL import Image

        with Image.open(path) as loaded:
            return _prepare_clipboard_image(loaded)
    except Exception:
        return None


def _encode_image_for_protocol(
    image: "Image.Image",
    *,
    preferred_mime: str | None,
) -> tuple[str, bytes] | None:
    has_alpha = image.mode in {"RGBA", "LA", "PA"} or ("transparency" in image.info)
    candidates: list[str] = []
    if preferred_mime in ALLOWED_IMAGE_MIME_TYPES:
        candidates.append(preferred_mime)
    if has_alpha:
        candidates.extend(["image/png", "image/webp", "image/jpeg"])
    else:
        candidates.extend(["image/jpeg", "image/webp", "image/png"])

    seen: set[str] = set()
    for mime_type in candidates:
        if mime_type in seen:
            continue
        seen.add(mime_type)
        if mime_type == "image/png":
            encoded = _save_image_bytes(image, "PNG", optimize=True)
            if encoded is not None and len(encoded) <= MAX_IMAGE_BYTES:
                return mime_type, encoded
        elif mime_type == "image/jpeg":
            rgb = image.convert("RGB")
            for quality in IMAGE_QUALITY_STEPS:
                encoded = _save_image_bytes(
                    rgb,
                    "JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                if encoded is not None and len(encoded) <= MAX_IMAGE_BYTES:
                    return mime_type, encoded
        elif mime_type == "image/webp":
            for quality in IMAGE_QUALITY_STEPS:
                encoded = _save_image_bytes(
                    image,
                    "WEBP",
                    quality=quality,
                    method=6,
                )
                if encoded is not None and len(encoded) <= MAX_IMAGE_BYTES:
                    return mime_type, encoded
    return None


def _save_image_bytes(image: "Image.Image", format_name: str, **save_kwargs: object) -> bytes | None:
    try:
        output = io.BytesIO()
        image.save(output, format=format_name, **save_kwargs)
        return output.getvalue()
    except Exception:
        return None


def _extract_exif_orientation_degrees(image: "Image.Image") -> int:
    try:
        exif = image.getexif()
    except Exception:
        return 0
    if not exif:
        return 0
    value = exif.get(0x0112)
    if value == 3:
        return 180
    if value == 6:
        return 90
    if value == 8:
        return 270
    return 0


def _compute_image_signature(image: Image.Image) -> str:
    thumb = image.copy()
    thumb.thumbnail((64, 64))
    if thumb.mode not in {"RGB", "RGBA"}:
        thumb = thumb.convert("RGBA")
    payload = (
        f"{image.size[0]}x{image.size[1]}|{thumb.size[0]}x{thumb.size[1]}".encode("utf-8")
        + thumb.tobytes()
    )
    return hashlib.sha256(payload).hexdigest()


def _clipboard_text_candidates(
    *,
    include_applescript_text: bool = True,
    include_file_url: bool = True,
) -> list[str]:
    candidates: list[str] = []
    try:
        import pyperclip

        value = pyperclip.paste()
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    except Exception:
        pass

    if include_applescript_text:
        try:
            result = subprocess.run(
                ["osascript", "-e", "get the clipboard as text"],
                check=False,
                capture_output=True,
                text=True,
                timeout=APPLESCRIPT_QUERY_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                candidates.append(result.stdout.strip())
        except Exception:
            pass

    if include_file_url:
        # Telegram and some apps expose file references via pasteboard file URL.
        file_url_path = _clipboard_file_url_path()
        if file_url_path is not None:
            candidates.append(str(file_url_path))

    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def _clipboard_file_url_path() -> Path | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", "get POSIX path of (the clipboard as «class furl»)"],
            check=False,
            capture_output=True,
            text=True,
            timeout=APPLESCRIPT_QUERY_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).expanduser()
    except Exception:
        return None
    return None


def _clipboard_text_to_path(raw_text: str) -> Path | None:
    text = raw_text.strip()
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urllib.parse.urlparse(text)
        path = urllib.parse.unquote(parsed.path)
        if path:
            return Path(path).expanduser()
    direct = Path(text).expanduser()
    if direct.is_absolute():
        return direct
    return None


def _finder_selected_path() -> Path | None:
    script = """
    tell application "Finder"
        if selection is not {} then
            set p to POSIX path of (item 1 of selection as alias)
            return p
        end if
    end tell
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=APPLESCRIPT_QUERY_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).expanduser()
    except Exception:
        return None
    return None


def _finder_selection_matches_clipboard_candidates(finder_path: Path, candidates: list[str]) -> bool:
    finder_resolved = _safe_resolve_path(finder_path)
    finder_name = finder_resolved.name.casefold()
    finder_suffix = finder_resolved.suffix.lower()
    if finder_suffix not in IMAGE_PATH_EXTENSIONS:
        return False

    for candidate in candidates:
        text = candidate.strip()
        if not text:
            continue

        candidate_path = _clipboard_text_to_path(text)
        if candidate_path is not None:
            if _safe_resolve_path(candidate_path) == finder_resolved:
                return True
            continue

        candidate_name = Path(text).name.casefold()
        candidate_suffix = Path(text).suffix.lower()
        if candidate_name == finder_name and candidate_suffix in IMAGE_PATH_EXTENSIONS:
            return True
    return False


def _safe_resolve_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve(strict=False)
    except Exception:
        return expanded


def _set_clipboard_png_bytes(png_bytes: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(png_bytes)
        temp_path = Path(handle.name)
    try:
        escaped_path = str(temp_path).replace("\\", "\\\\").replace('"', '\\"')
        script = f'set the clipboard to (read (POSIX file "{escaped_path}") as «class PNGf»)'
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=APPLESCRIPT_SET_CLIPBOARD_TIMEOUT_SECONDS,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def _normalize_mime_type(mime_type: str | None) -> str | None:
    if mime_type is None:
        return None
    normalized = mime_type.strip().lower()
    if normalized == "image/jpg":
        return "image/jpeg"
    return normalized if normalized in ALLOWED_IMAGE_MIME_TYPES else None
