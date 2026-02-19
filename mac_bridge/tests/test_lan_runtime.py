from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import socket
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.clipboard_io import ClipboardImage, InMemoryClipboardBackend
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


class TransformingClipboardBackend:
    """Backend that rewrites image payloads to simulate clipboard normalization."""

    def __init__(self) -> None:
        self.text = ""
        self.image: ClipboardImage | None = None

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def get_image(self) -> ClipboardImage | None:
        return self.image

    def set_image(self, image: ClipboardImage) -> None:
        # Simulate format conversion/normalization on clipboard write.
        self.image = ClipboardImage(
            mime_type="image/png",
            data=image.data + b".normalized",
            width=image.width,
            height=image.height,
            orientation=0,
            signature="normalized-signature",
        )


def _http_get_status_and_body(url: str) -> tuple[int, str]:
    with urlopen(url, timeout=2) as response:
        body = response.read().decode("utf-8")
        return int(response.status), body


def _http_get_error_status(url: str) -> int:
    try:
        _http_get_status_and_body(url)
    except HTTPError as exc:
        # Ensure the error response body is consumed/closed.
        _ = exc.read()
        status = int(exc.code)
        exc.close()
        return status
    raise AssertionError("Expected HTTPError")


@unittest.skipUnless(WEBSOCKETS_AVAILABLE, "websockets is required for LAN runtime tests")
class LanRuntimeReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_code_redeem_is_single_use(self) -> None:
        logger = logging.getLogger("lan-runtime-test-pairing")
        logger.addHandler(logging.NullHandler())
        clipboard = InMemoryClipboardBackend()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path, logger=logger)
            store.set_token("phase8-test-token")

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
                pairing_enabled=True,
            )
            offer = server.issue_pairing_offer()
            self.assertIsNotNone(offer)
            assert offer is not None

            server_task = asyncio.create_task(server.run())
            await asyncio.sleep(0.2)

            redeem_url = (
                f"http://127.0.0.1:{port}/v1/pair/redeem"
                f"?code={quote(offer.code)}"
                "&source=android"
                "&device_id=android.pixel7.pairing"
            )

            try:
                status, body = await asyncio.to_thread(_http_get_status_and_body, redeem_url)
                self.assertEqual(status, HTTPStatus.OK)
                payload = json.loads(body)
                self.assertEqual(payload.get("token"), "phase8-test-token")
                self.assertEqual(payload.get("ws_path"), "/v1/clipboard")
                self.assertEqual(payload.get("port"), port)

                second_status, second_body = await asyncio.to_thread(_http_get_status_and_body, redeem_url)
                self.assertEqual(second_status, HTTPStatus.OK)
                second_payload = json.loads(second_body)
                self.assertEqual(second_payload.get("token"), "phase8-test-token")

                redeem_url_other_device = (
                    f"http://127.0.0.1:{port}/v1/pair/redeem"
                    f"?code={quote(offer.code)}"
                    "&source=android"
                    "&device_id=android.pixel7.other"
                )
                third_status = await asyncio.to_thread(_http_get_error_status, redeem_url_other_device)
                self.assertEqual(third_status, HTTPStatus.GONE)
            finally:
                server.stop()
                await asyncio.wait_for(server_task, timeout=5)

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

    async def test_image_sync_sets_local_clipboard_and_acks(self) -> None:
        logger = logging.getLogger("lan-runtime-test-image")
        logger.addHandler(logging.NullHandler())
        clipboard = InMemoryClipboardBackend()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path, logger=logger)
            store.set_token("phase7-test-token")

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
                "Authorization": "Bearer phase7-test-token",
                "X-Clipboard-Protocol-Version": "1.0",
                "X-Clipboard-Device-Id": "android.pixel7.image",
                "X-Clipboard-Source": "android",
            }
            uri = f"ws://127.0.0.1:{port}/v1/clipboard"

            image_bytes = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00"
                b"\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            image_event = normalize_event(
                device_id="android.pixel7.image",
                source="android",
                event_type="set_image",
                payload={
                    "mime_type": "image/png",
                    "byte_size": len(image_bytes),
                    "data_base64": base64.b64encode(image_bytes).decode("ascii"),
                    "width": 1,
                    "height": 1,
                    "orientation": 0,
                },
            )

            try:
                async with connect(uri, additional_headers=headers) as ws:
                    await ws.send(json.dumps(image_event))
                    raw_ack = await asyncio.wait_for(ws.recv(), timeout=2)
                    ack = json.loads(raw_ack)
                    self.assertEqual(ack["event_type"], "ack")
                    self.assertEqual(ack["payload"]["status"], "accepted")
                self.assertIsNotNone(clipboard.get_image())
                self.assertEqual(clipboard.get_image().mime_type, "image/png")
            finally:
                server.stop()
                await asyncio.wait_for(server_task, timeout=5)

    async def test_image_sync_does_not_echo_transformed_clipboard_image(self) -> None:
        logger = logging.getLogger("lan-runtime-test-image-echo")
        logger.addHandler(logging.NullHandler())
        clipboard = TransformingClipboardBackend()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path, logger=logger)
            store.set_token("phase7-test-token-echo")

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
                "Authorization": "Bearer phase7-test-token-echo",
                "X-Clipboard-Protocol-Version": "1.0",
                "X-Clipboard-Device-Id": "android.pixel7.image.echo",
                "X-Clipboard-Source": "android",
            }
            uri = f"ws://127.0.0.1:{port}/v1/clipboard"

            image_bytes = b"phase7-image-bytes"
            image_event = normalize_event(
                device_id="android.pixel7.image.echo",
                source="android",
                event_type="set_image",
                payload={
                    "mime_type": "image/jpeg",
                    "byte_size": len(image_bytes),
                    "data_base64": base64.b64encode(image_bytes).decode("ascii"),
                    "width": 10,
                    "height": 5,
                    "orientation": 90,
                },
            )

            try:
                async with connect(uri, additional_headers=headers) as ws:
                    await ws.send(json.dumps(image_event))
                    raw_ack = await asyncio.wait_for(ws.recv(), timeout=2)
                    ack = json.loads(raw_ack)
                    self.assertEqual(ack["event_type"], "ack")
                    self.assertEqual(ack["payload"]["status"], "accepted")

                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), timeout=0.3)
            finally:
                server.stop()
                await asyncio.wait_for(server_task, timeout=5)


if __name__ == "__main__":
    unittest.main()
