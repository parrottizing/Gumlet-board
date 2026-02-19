from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.clipboard_io import (
    ClipboardImage,
    MacClipboardBackend,
    _finder_selection_matches_clipboard_candidates,
)


class _FakePILImage:
    def __init__(self, exif_orientation: int | None = None) -> None:
        self.exif_orientation = exif_orientation
        self.rotate_calls: list[int] = []

    def __enter__(self) -> "_FakePILImage":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def getexif(self) -> dict[int, int]:
        if self.exif_orientation is None:
            return {}
        return {0x0112: self.exif_orientation}

    def rotate(self, _degrees: int, expand: bool = True) -> "_FakePILImage":
        _ = expand
        self.rotate_calls.append(_degrees)
        return self

    def save(self, output, format: str) -> None:
        _ = format
        output.write(b"\x89PNG\r\n\x1a\n")


class ClipboardIoTests(unittest.TestCase):
    def test_set_image_accepts_image_jpg_alias(self) -> None:
        backend = MacClipboardBackend()
        image = ClipboardImage(
            mime_type="image/jpg",
            data=b"not-a-real-jpeg",
            width=1,
            height=1,
            orientation=0,
            signature="sig",
        )

        fake_pil = types.ModuleType("PIL")
        fake_pil.Image = types.SimpleNamespace(open=lambda _stream: _FakePILImage())
        fake_pil.ImageOps = types.SimpleNamespace(exif_transpose=lambda opened: opened)

        with patch.dict(sys.modules, {"PIL": fake_pil}):
            with patch("bridge.clipboard_io._set_clipboard_png_bytes") as set_clipboard_bytes:
                backend.set_image(image)
        set_clipboard_bytes.assert_called_once()

    def test_set_image_skips_protocol_rotation_when_matching_exif_orientation(self) -> None:
        backend = MacClipboardBackend()
        image = ClipboardImage(
            mime_type="image/jpeg",
            data=b"not-a-real-jpeg",
            width=10,
            height=20,
            orientation=90,
            signature="sig",
        )
        fake_opened = _FakePILImage(exif_orientation=6)

        fake_pil = types.ModuleType("PIL")
        fake_pil.Image = types.SimpleNamespace(open=lambda _stream: fake_opened)
        fake_pil.ImageOps = types.SimpleNamespace(exif_transpose=lambda opened: opened)

        with patch.dict(sys.modules, {"PIL": fake_pil}):
            with patch("bridge.clipboard_io._set_clipboard_png_bytes"):
                backend.set_image(image)

        self.assertEqual(fake_opened.rotate_calls, [])

    def test_set_image_applies_protocol_rotation_without_exif_orientation(self) -> None:
        backend = MacClipboardBackend()
        image = ClipboardImage(
            mime_type="image/jpeg",
            data=b"not-a-real-jpeg",
            width=10,
            height=20,
            orientation=90,
            signature="sig",
        )
        fake_opened = _FakePILImage(exif_orientation=None)

        fake_pil = types.ModuleType("PIL")
        fake_pil.Image = types.SimpleNamespace(open=lambda _stream: fake_opened)
        fake_pil.ImageOps = types.SimpleNamespace(exif_transpose=lambda opened: opened)

        with patch.dict(sys.modules, {"PIL": fake_pil}):
            with patch("bridge.clipboard_io._set_clipboard_png_bytes"):
                backend.set_image(image)

        self.assertEqual(fake_opened.rotate_calls, [-90])

    def test_get_image_prefers_file_reference_over_direct_placeholder(self) -> None:
        backend = MacClipboardBackend()
        placeholder = _FakePILImage()
        expected = ClipboardImage(
            mime_type="image/png",
            data=b"png",
            width=1,
            height=1,
            orientation=0,
            signature="sig",
        )

        fake_pil = types.ModuleType("PIL")
        fake_pil.Image = types.SimpleNamespace(Image=_FakePILImage)
        fake_pil.ImageGrab = types.SimpleNamespace(grabclipboard=lambda: placeholder)

        with patch.dict(sys.modules, {"PIL": fake_pil}):
            with patch(
                "bridge.clipboard_io._clipboard_text_candidates",
                return_value=["/Users/example/Pictures/photo.png"],
            ):
                with patch(
                    "bridge.clipboard_io._load_clipboard_image_from_path",
                    return_value=expected,
                ):
                    with patch("bridge.clipboard_io._prepare_clipboard_image") as prepare:
                        actual = backend.get_image()

        self.assertIs(actual, expected)
        prepare.assert_not_called()

    def test_get_image_skips_finder_probe_when_direct_image_exists(self) -> None:
        backend = MacClipboardBackend()
        direct_image = _FakePILImage()
        expected = ClipboardImage(
            mime_type="image/png",
            data=b"png",
            width=1,
            height=1,
            orientation=0,
            signature="sig",
        )

        fake_pil = types.ModuleType("PIL")
        fake_pil.Image = types.SimpleNamespace(Image=_FakePILImage)
        fake_pil.ImageGrab = types.SimpleNamespace(grabclipboard=lambda: direct_image)

        with patch.dict(sys.modules, {"PIL": fake_pil}):
            with patch(
                "bridge.clipboard_io._clipboard_text_candidates",
                return_value=[],
            ) as text_candidates:
                with patch("bridge.clipboard_io._finder_selected_path") as finder:
                    with patch(
                        "bridge.clipboard_io._prepare_clipboard_image",
                        return_value=expected,
                    ):
                        actual = backend.get_image()

        self.assertIs(actual, expected)
        text_candidates.assert_called_once_with(
            include_applescript_text=False,
            include_file_url=False,
        )
        finder.assert_not_called()

    def test_finder_selection_requires_clipboard_match(self) -> None:
        finder_path = Path("/Users/example/Pictures/photo.png")
        self.assertFalse(
            _finder_selection_matches_clipboard_candidates(
                finder_path,
                ["just some copied text"],
            )
        )

    def test_finder_selection_accepts_matching_filename(self) -> None:
        finder_path = Path("/Users/example/Pictures/photo.png")
        self.assertTrue(
            _finder_selection_matches_clipboard_candidates(
                finder_path,
                ["photo.png"],
            )
        )

    def test_finder_selection_accepts_matching_absolute_path(self) -> None:
        finder_path = Path("/Users/example/Pictures/photo.png")
        self.assertTrue(
            _finder_selection_matches_clipboard_candidates(
                finder_path,
                [str(finder_path)],
            )
        )


if __name__ == "__main__":
    unittest.main()
