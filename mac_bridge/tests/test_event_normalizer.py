from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.event_normalizer import ProtocolError, compute_payload_hash, normalize_event, validate_event


class EventNormalizerTests(unittest.TestCase):
    def test_normalize_and_validate_set_text(self) -> None:
        event = normalize_event(
            device_id="android.pixel7.test",
            source="android",
            event_type="set_text",
            payload={"mime_type": "text/plain", "text": "hello", "is_sensitive": False},
        )
        validate_event(event)

    def test_hash_mismatch_is_rejected(self) -> None:
        event = normalize_event(
            device_id="android.pixel7.test",
            source="android",
            event_type="set_text",
            payload={"mime_type": "text/plain", "text": "hello", "is_sensitive": False},
        )
        event["payload_hash"] = "0" * 64
        with self.assertRaises(ProtocolError) as err:
            validate_event(event)
        self.assertEqual(err.exception.code, "HASH_MISMATCH")

    def test_normalize_and_validate_set_image(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nphase7"
        event = normalize_event(
            device_id="android.pixel7.test",
            source="android",
            event_type="set_image",
            payload={
                "mime_type": "image/png",
                "byte_size": len(image_bytes),
                "data_base64": base64.b64encode(image_bytes).decode("ascii"),
                "width": 640,
                "height": 480,
                "orientation": 0,
            },
        )
        validate_event(event)

    def test_set_image_invalid_byte_size_is_rejected(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nphase7"
        event = normalize_event(
            device_id="android.pixel7.test",
            source="android",
            event_type="set_image",
            payload={
                "mime_type": "image/png",
                "byte_size": len(image_bytes),
                "data_base64": base64.b64encode(image_bytes).decode("ascii"),
                "width": 640,
                "height": 480,
                "orientation": 0,
            },
        )
        event["payload"]["byte_size"] = len(image_bytes) + 1
        event["payload_hash"] = compute_payload_hash(event["payload"])
        with self.assertRaises(ProtocolError) as err:
            validate_event(event)
        self.assertEqual(err.exception.code, "BAD_MESSAGE")


if __name__ == "__main__":
    unittest.main()
