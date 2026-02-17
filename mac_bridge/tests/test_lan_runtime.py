from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.clipboard_io import InMemoryClipboardBackend
from bridge.event_normalizer import normalize_event
from bridge.state_store import StateStore

WEBSOCKETS_AVAILABLE = importlib.util.find_spec("websockets") is not None

if WEBSOCKETS_AVAILABLE:
    from websockets.asyncio.client import connect

    from bridge.transport_lan import LanClipboardServer


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


@unittest.skipUnless(WEBSOCKETS_AVAILABLE, "websockets is required for LAN runtime tests")
class LanRuntimeReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_sync_survives_reconnect(self) -> None:
        logger = logging.getLogger("lan-runtime-test")
        logger.addHandler(logging.NullHandler())
        clipboard = InMemoryClipboardBackend()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path, logger=logger)
            store.set_token("phase2-test-token")

            port = _pick_free_port()
            server = LanClipboardServer(
                host="127.0.0.1",
                port=port,
                path="/v1/clipboard",
                state_store=store,
                clipboard=clipboard,
                logger=logger,
                advertise_mdns=False,
                poll_interval=0.05,
            )

            server_task = asyncio.create_task(server.run())
            await asyncio.sleep(0.2)

            headers = {
                "Authorization": "Bearer phase2-test-token",
                "X-Clipboard-Protocol-Version": "1.0",
                "X-Clipboard-Device-Id": "android.pixel7.test",
                "X-Clipboard-Source": "android",
            }
            uri = f"ws://127.0.0.1:{port}/v1/clipboard"

            try:
                first_event = normalize_event(
                    device_id="android.pixel7.test",
                    source="android",
                    event_type="set_text",
                    payload={
                        "mime_type": "text/plain",
                        "text": "phase-2 first message",
                        "is_sensitive": False,
                    },
                )
                async with connect(uri, additional_headers=headers) as ws:
                    await ws.send(json.dumps(first_event))
                    raw_ack = await asyncio.wait_for(ws.recv(), timeout=2)
                    ack = json.loads(raw_ack)
                    self.assertEqual(ack["event_type"], "ack")
                    self.assertEqual(ack["payload"]["status"], "accepted")
                self.assertEqual(clipboard.get_text(), "phase-2 first message")

                second_event = normalize_event(
                    device_id="android.pixel7.test",
                    source="android",
                    event_type="set_text",
                    payload={
                        "mime_type": "text/plain",
                        "text": "phase-2 second message",
                        "is_sensitive": False,
                    },
                )
                async with connect(uri, additional_headers=headers) as ws:
                    await ws.send(json.dumps(second_event))
                    raw_ack = await asyncio.wait_for(ws.recv(), timeout=2)
                    ack = json.loads(raw_ack)
                    self.assertEqual(ack["event_type"], "ack")
                    self.assertEqual(ack["payload"]["status"], "accepted")
                self.assertEqual(clipboard.get_text(), "phase-2 second message")
            finally:
                server.stop()
                await asyncio.wait_for(server_task, timeout=5)


if __name__ == "__main__":
    unittest.main()
