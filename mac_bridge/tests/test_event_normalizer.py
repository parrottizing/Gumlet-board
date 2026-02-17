from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.event_normalizer import ProtocolError, normalize_event, validate_event


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


if __name__ == "__main__":
    unittest.main()
