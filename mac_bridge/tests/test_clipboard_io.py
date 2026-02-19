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
    def __enter__(self) -> "_FakePILImage":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def rotate(self, _degrees: int, expand: bool = True) -> "_FakePILImage":
        _ = expand
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
