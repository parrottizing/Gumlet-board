from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import signal
import socket
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .clipboard_io import ClipboardBackend, ClipboardImage, ClipboardPoller
from .event_normalizer import (
    DEVICE_ID_PATTERN,
    ProtocolError,
    compute_payload_hash,
    make_ack,
    make_error,
    normalize_event,
    parse_json_message,
    validate_event,
)
from .mdns import MdnsAdvertiser
from .state_store import StateStore


@dataclass(slots=True)
class SessionMeta:
    session_id: str
    device_id: str
    source: str
    connected_at_ms: int


def _now_ms() -> int:
    return int(time.time() * 1000)


PAIRING_REDEEM_REPLAY_WINDOW_SECONDS = 30


def _infer_pairing_host(bind_host: str) -> str:
    normalized = bind_host.strip().strip("[]")
    if normalized and normalized not in {"0.0.0.0", "::", "::0"}:
        return normalized
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


@dataclass(slots=True, frozen=True)
class PairingOffer:
    deep_link: str
    code: str
    expires_at_ms: int


class LanClipboardServer:
    """LAN daemon runtime for clipboard sync over WebSocket."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        path: str,
        state_store: StateStore,
        clipboard: ClipboardBackend,
        logger,
        advertise_mdns: bool = True,
        mdns_service_type: str = "_gumlet-clipboard._tcp",
        poll_interval: float = 0.4,
        pairing_enabled: bool = True,
        pairing_host: str = "",
        pairing_code_ttl_seconds: int = 180,
        pairing_redeem_path: str = "/v1/pair/redeem",
    ) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.state_store = state_store
        self.clipboard = clipboard
        self.logger = logger
        self.advertise_mdns = advertise_mdns
        self.mdns_service_type = mdns_service_type
        self.poller = ClipboardPoller(clipboard, poll_interval=poll_interval, logger=logger)
        self.pairing_enabled = pairing_enabled
        self.pairing_code_ttl_seconds = max(30, int(pairing_code_ttl_seconds))
        normalized_redeem_path = pairing_redeem_path.strip() or "/v1/pair/redeem"
        if not normalized_redeem_path.startswith("/"):
            normalized_redeem_path = f"/{normalized_redeem_path}"
        self.pairing_redeem_path = normalized_redeem_path
        self.pairing_host = pairing_host.strip() if pairing_host.strip() else _infer_pairing_host(host)
        self._pairing_codes: dict[str, int] = {}
        self._consumed_pairing_codes: dict[str, tuple[int, str]] = {}

        self._stop_event = asyncio.Event()
        self._sessions_by_device: dict[str, set[ServerConnection]] = defaultdict(set)
        self._session_meta: dict[ServerConnection, SessionMeta] = {}
        self._session_lock = asyncio.Lock()

        self._recent_event_ids: dict[str, float] = {}
        self._recent_remote_hashes: deque[tuple[str, float]] = deque()
        self._event_ttl_seconds = 10 * 60
        self._hash_ttl_seconds = 30

        self._mdns_advertiser = MdnsAdvertiser(
            service_name=self.state_store.service_identity,
            service_type=self.mdns_service_type,
            port=self.port,
            txt_records={
                "protocol": "1.0",
                "path": self.path,
                "service_id": self.state_store.service_identity,
            },
            logger=self.logger,
        )

    def stop(self) -> None:
        self._stop_event.set()

    def active_device_sessions(self) -> dict[str, int]:
        return {
            device_id: len(connections)
            for device_id, connections in self._sessions_by_device.items()
            if connections
        }

    def _prune_expired(self) -> None:
        cutoff = time.monotonic() - self._event_ttl_seconds
        stale_keys = [key for key, ts in self._recent_event_ids.items() if ts < cutoff]
        for key in stale_keys:
            self._recent_event_ids.pop(key, None)

        hash_cutoff = time.monotonic() - self._hash_ttl_seconds
        while self._recent_remote_hashes and self._recent_remote_hashes[0][1] < hash_cutoff:
            self._recent_remote_hashes.popleft()

    def _mark_seen_event(self, device_id: str, event_id: str) -> None:
        self._recent_event_ids[f"{device_id}:{event_id}"] = time.monotonic()
        self._prune_expired()

    def _has_seen_event(self, device_id: str, event_id: str) -> bool:
        self._prune_expired()
        if f"{device_id}:{event_id}" in self._recent_event_ids:
            return True
        persisted = self.state_store.get_device_sync_state(device_id)
        return persisted.get("last_event_id") == event_id

    def _mark_remote_hash(self, payload_hash: str) -> None:
        self._recent_remote_hashes.append((payload_hash, time.monotonic()))
        self._prune_expired()

    def _is_recent_remote_hash(self, payload_hash: str) -> bool:
        self._prune_expired()
        return any(h == payload_hash for h, _ts in self._recent_remote_hashes)

    def _prune_pairing_codes(self, now_ms: int | None = None) -> None:
        now = _now_ms() if now_ms is None else now_ms
        expired_codes = [code for code, expires_at_ms in self._pairing_codes.items() if expires_at_ms <= now]
        for code in expired_codes:
            self._pairing_codes.pop(code, None)
        stale_consumed_codes = [
            code for code, (replay_until_ms, _device_id) in self._consumed_pairing_codes.items()
            if replay_until_ms <= now
        ]
        for code in stale_consumed_codes:
            self._consumed_pairing_codes.pop(code, None)

    def issue_pairing_offer(self) -> PairingOffer | None:
        if not self.pairing_enabled:
            return None
        now_ms = _now_ms()
        self._prune_pairing_codes(now_ms)
        code = secrets.token_urlsafe(18)
        expires_at_ms = now_ms + (self.pairing_code_ttl_seconds * 1000)
        self._pairing_codes[code] = expires_at_ms
        deep_link = self._build_pairing_deep_link(code)
        return PairingOffer(
            deep_link=deep_link,
            code=code,
            expires_at_ms=expires_at_ms,
        )

    def _build_pairing_deep_link(self, code: str) -> str:
        query = urlencode(
            {
                "lan_pair_version": "1",
                "lan_pair_host": self.pairing_host,
                "lan_pair_port": str(self.port),
                "lan_pair_code": code,
                "lan_pair_redeem_path": self.pairing_redeem_path,
            }
        )
        return f"ui://florisboard/settings/clipboard?{query}"

    def _consume_pairing_code(self, code: str, device_id: str) -> str:
        if not self.pairing_enabled or not code or not device_id:
            return "invalid"
        now_ms = _now_ms()
        self._prune_pairing_codes(now_ms)
        replay_state = self._consumed_pairing_codes.get(code)
        if replay_state is not None:
            replay_until_ms, replay_device_id = replay_state
            if replay_until_ms > now_ms and replay_device_id == device_id:
                return "replay"
            if replay_until_ms > now_ms and replay_device_id != device_id:
                return "invalid"
        expires_at_ms = self._pairing_codes.pop(code, None)
        if expires_at_ms is None or expires_at_ms <= now_ms:
            return "invalid"
        replay_until_ms = now_ms + (PAIRING_REDEEM_REPLAY_WINDOW_SECONDS * 1000)
        self._consumed_pairing_codes[code] = (replay_until_ms, device_id)
        return "accepted"

    def _pairing_response_body(self) -> str:
        payload = {
            "protocol_version": "1",
            "token": self.state_store.token,
            "host": self.pairing_host,
            "port": self.port,
            "ws_path": self.path,
            "service_id": self.state_store.service_identity,
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"

    async def _process_pairing_redeem(self, connection: ServerConnection, request_path: str):
        if not self.pairing_enabled:
            return connection.respond(HTTPStatus.NOT_FOUND, "PAIRING_DISABLED\n")

        parsed = urlparse(request_path)
        params = parse_qs(parsed.query)
        code = params.get("code", [""])[0].strip()
        if not code:
            return connection.respond(HTTPStatus.BAD_REQUEST, "PAIRING_CODE_MISSING\n")
        device_id = params.get("device_id", [""])[0].strip()
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            return connection.respond(HTTPStatus.BAD_REQUEST, "BAD_DEVICE_ID\n")

        consume_result = self._consume_pairing_code(code, device_id)
        if consume_result == "invalid":
            return connection.respond(HTTPStatus.GONE, "PAIRING_CODE_INVALID\n")

        return connection.respond(HTTPStatus.OK, self._pairing_response_body())

    async def _process_request(self, connection: ServerConnection, request):
        parsed = urlparse(request.path)
        request_path = parsed.path
        if request_path == "/healthz":
            return connection.respond(HTTPStatus.OK, "OK\n")
        if request_path == self.pairing_redeem_path:
            return await self._process_pairing_redeem(connection, request.path)
        if request_path != self.path:
            return connection.respond(HTTPStatus.NOT_FOUND, "NOT_FOUND\n")

        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.state_store.token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "AUTH_FAILURE\n")

        version = request.headers.get("X-Clipboard-Protocol-Version", "")
        if not isinstance(version, str) or not version.startswith("1."):
            return connection.respond(HTTPStatus.BAD_REQUEST, "VERSION_MISMATCH\n")

        device_id = request.headers.get("X-Clipboard-Device-Id", "").strip()
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            return connection.respond(HTTPStatus.BAD_REQUEST, "BAD_DEVICE_ID\n")

        source = request.headers.get("X-Clipboard-Source", "").strip()
        if source not in {"android", "mac"}:
            return connection.respond(HTTPStatus.BAD_REQUEST, "BAD_SOURCE\n")

        return None

    async def _register_session(self, websocket: ServerConnection) -> None:
        headers = websocket.request.headers
        device_id = headers["X-Clipboard-Device-Id"]
        source = headers["X-Clipboard-Source"]
        session_id = f"{device_id}-{_now_ms()}"

        meta = SessionMeta(
            session_id=session_id,
            device_id=device_id,
            source=source,
            connected_at_ms=_now_ms(),
        )
        async with self._session_lock:
            self._session_meta[websocket] = meta
            self._sessions_by_device[device_id].add(websocket)
            active_sessions = len(self._session_meta)

        self.state_store.record_paired_device(device_id, source)
        self.logger.info(
            "Session connected",
            extra={
                "event": "session.connected",
                "session_id": meta.session_id,
                "device_id": device_id,
                "status": str(active_sessions),
            },
        )

    async def _unregister_session(self, websocket: ServerConnection) -> None:
        async with self._session_lock:
            meta = self._session_meta.pop(websocket, None)
            if meta is None:
                return
            device_sessions = self._sessions_by_device.get(meta.device_id)
            if device_sessions is not None:
                device_sessions.discard(websocket)
                if not device_sessions:
                    self._sessions_by_device.pop(meta.device_id, None)
            active_sessions = len(self._session_meta)

        self.logger.info(
            "Session disconnected",
            extra={
                "event": "session.disconnected",
                "session_id": meta.session_id,
                "device_id": meta.device_id,
                "status": str(active_sessions),
            },
        )

    async def _send_event(self, websocket: ServerConnection, event: dict[str, Any]) -> None:
        await websocket.send(json.dumps(event, separators=(",", ":"), ensure_ascii=False))

    async def _broadcast_text(
        self,
        *,
        text: str,
        is_sensitive: bool = False,
        exclude: ServerConnection | None = None,
    ) -> None:
        payload = {"mime_type": "text/plain", "text": text, "is_sensitive": is_sensitive}
        event = normalize_event(
            device_id=self.state_store.service_identity,
            source="mac",
            event_type="set_text",
            payload=payload,
        )

        async with self._session_lock:
            connections = list(self._session_meta.keys())

        for conn in connections:
            if exclude is not None and conn is exclude:
                continue
            try:
                await self._send_event(conn, event)
            except ConnectionClosed:
                continue

    def _build_image_payload(self, image: ClipboardImage) -> dict[str, Any]:
        return {
            "mime_type": image.mime_type,
            "byte_size": len(image.data),
            "data_base64": base64.b64encode(image.data).decode("ascii"),
            "width": image.width,
            "height": image.height,
            "orientation": image.orientation,
        }

    async def _broadcast_image(
        self,
        *,
        image: ClipboardImage,
        exclude: ServerConnection | None = None,
    ) -> None:
        payload = self._build_image_payload(image)
        event = normalize_event(
            device_id=self.state_store.service_identity,
            source="mac",
            event_type="set_image",
            payload=payload,
        )

        async with self._session_lock:
            connections = list(self._session_meta.keys())

        for conn in connections:
            if exclude is not None and conn is exclude:
                continue
            try:
                await self._send_event(conn, event)
            except ConnectionClosed:
                continue

    async def _on_local_text_changed(self, text: str) -> None:
        if not text.strip():
            return
        payload = {"mime_type": "text/plain", "text": text, "is_sensitive": False}
        payload_hash = compute_payload_hash(payload)
        if self._is_recent_remote_hash(payload_hash):
            self.logger.debug(
                "Skipping local clipboard echo",
                extra={"event": "clipboard.local.echo_suppressed"},
            )
            return
        await self._broadcast_text(text=text)

    async def _on_local_image_changed(self, image: ClipboardImage) -> None:
        payload = self._build_image_payload(image)
        payload_hash = compute_payload_hash(payload)
        if self._is_recent_remote_hash(payload_hash):
            self.logger.debug(
                "Skipping local clipboard image echo",
                extra={"event": "clipboard.local.image_echo_suppressed"},
            )
            return
        await self._broadcast_image(image=image)

    async def _handle_set_text(
        self,
        websocket: ServerConnection,
        meta: SessionMeta,
        envelope: dict[str, Any],
    ) -> None:
        event_id = envelope["event_id"]
        payload_hash = envelope["payload_hash"]

        if self._has_seen_event(meta.device_id, event_id):
            ack = make_ack(
                device_id=self.state_store.service_identity,
                source="mac",
                acked_event_id=event_id,
                status="duplicate",
            )
            await self._send_event(websocket, ack)
            return

        payload = envelope["payload"]
        text = payload["text"]
        if self._is_recent_remote_hash(payload_hash):
            ack = make_ack(
                device_id=self.state_store.service_identity,
                source="mac",
                acked_event_id=event_id,
                status="duplicate",
            )
            await self._send_event(websocket, ack)
            return

        try:
            self.clipboard.set_text(text)
        except Exception as exc:
            ack = make_ack(
                device_id=self.state_store.service_identity,
                source="mac",
                acked_event_id=event_id,
                status="rejected",
                error_code="TEMPORARY_UNAVAILABLE",
            )
            await self._send_event(websocket, ack)
            error_event = make_error(
                device_id=self.state_store.service_identity,
                source="mac",
                code="TEMPORARY_UNAVAILABLE",
                message="Failed to write local clipboard",
                retryable=True,
            )
            await self._send_event(websocket, error_event)
            self.logger.warning(
                "Failed applying inbound clipboard text",
                extra={
                    "event": "clipboard.inbound.apply_failed",
                    "device_id": meta.device_id,
                    "event_id": event_id,
                },
                exc_info=exc,
            )
            return

        self._mark_seen_event(meta.device_id, event_id)
        self._mark_remote_hash(payload_hash)
        self.state_store.update_device_sync_state(
            meta.device_id,
            last_event_id=event_id,
            last_payload_hash=payload_hash,
        )

        ack = make_ack(
            device_id=self.state_store.service_identity,
            source="mac",
            acked_event_id=event_id,
            status="accepted",
        )
        await self._send_event(websocket, ack)
        await self._broadcast_text(
            text=text,
            is_sensitive=bool(payload.get("is_sensitive", False)),
            exclude=websocket,
        )

    async def _handle_set_image(
        self,
        websocket: ServerConnection,
        meta: SessionMeta,
        envelope: dict[str, Any],
    ) -> None:
        event_id = envelope["event_id"]
        payload_hash = envelope["payload_hash"]

        if self._has_seen_event(meta.device_id, event_id):
            ack = make_ack(
                device_id=self.state_store.service_identity,
                source="mac",
                acked_event_id=event_id,
                status="duplicate",
            )
            await self._send_event(websocket, ack)
            return

        payload = envelope["payload"]
        if self._is_recent_remote_hash(payload_hash):
            ack = make_ack(
                device_id=self.state_store.service_identity,
                source="mac",
                acked_event_id=event_id,
                status="duplicate",
            )
            await self._send_event(websocket, ack)
            return

        applied_payload_hash: str | None = None
        try:
            image_bytes = base64.b64decode(payload["data_base64"], validate=True)
            image = ClipboardImage(
                mime_type=str(payload["mime_type"]),
                data=image_bytes,
                width=int(payload["width"]),
                height=int(payload["height"]),
                orientation=int(payload["orientation"]),
                signature=hashlib.sha256(image_bytes).hexdigest(),
            )
            self.clipboard.set_image(image)
            try:
                applied_image = self.clipboard.get_image()
            except Exception:
                applied_image = None
            if applied_image is not None:
                applied_payload_hash = compute_payload_hash(self._build_image_payload(applied_image))
        except Exception as exc:
            ack = make_ack(
                device_id=self.state_store.service_identity,
                source="mac",
                acked_event_id=event_id,
                status="rejected",
                error_code="TEMPORARY_UNAVAILABLE",
            )
            await self._send_event(websocket, ack)
            error_event = make_error(
                device_id=self.state_store.service_identity,
                source="mac",
                code="TEMPORARY_UNAVAILABLE",
                message="Failed to write local clipboard image",
                retryable=True,
            )
            await self._send_event(websocket, error_event)
            self.logger.warning(
                "Failed applying inbound clipboard image",
                extra={
                    "event": "clipboard.inbound.image_apply_failed",
                    "device_id": meta.device_id,
                    "event_id": event_id,
                },
                exc_info=exc,
            )
            return

        self._mark_seen_event(meta.device_id, event_id)
        self._mark_remote_hash(payload_hash)
        if applied_payload_hash is not None and applied_payload_hash != payload_hash:
            self._mark_remote_hash(applied_payload_hash)
        self.state_store.update_device_sync_state(
            meta.device_id,
            last_event_id=event_id,
            last_payload_hash=payload_hash,
        )

        ack = make_ack(
            device_id=self.state_store.service_identity,
            source="mac",
            acked_event_id=event_id,
            status="accepted",
        )
        await self._send_event(websocket, ack)
        await self._broadcast_image(image=image, exclude=websocket)

    async def _handle_message(
        self,
        websocket: ServerConnection,
        meta: SessionMeta,
        raw_message: str,
    ) -> None:
        try:
            envelope = parse_json_message(raw_message)
            validate_event(envelope)
        except ProtocolError as exc:
            error_event = make_error(
                device_id=self.state_store.service_identity,
                source="mac",
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            )
            await self._send_event(websocket, error_event)
            return

        if envelope["device_id"] != meta.device_id:
            error_event = make_error(
                device_id=self.state_store.service_identity,
                source="mac",
                code="BAD_MESSAGE",
                message="device_id does not match authenticated session",
            )
            await self._send_event(websocket, error_event)
            return

        event_type = envelope["event_type"]
        if event_type == "ping":
            pong = normalize_event(
                device_id=self.state_store.service_identity,
                source="mac",
                event_type="pong",
                payload={"received_event_id": envelope["event_id"]},
            )
            await self._send_event(websocket, pong)
            return

        if event_type == "set_text":
            await self._handle_set_text(websocket, meta, envelope)
            return

        if event_type == "set_image":
            await self._handle_set_image(websocket, meta, envelope)
            return

        # Ack/error/pong from clients can be ignored at daemon side.
        self.logger.debug(
            "Ignoring inbound event",
            extra={
                "event": "event.ignored",
                "event_type": event_type,
                "device_id": meta.device_id,
            },
        )

    async def _handler(self, websocket: ServerConnection) -> None:
        await self._register_session(websocket)
        try:
            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    error_event = make_error(
                        device_id=self.state_store.service_identity,
                        source="mac",
                        code="UNSUPPORTED_TYPE",
                        message="Binary frames are unsupported in v1 mode",
                    )
                    await self._send_event(websocket, error_event)
                    continue
                meta = self._session_meta.get(websocket)
                if meta is None:
                    continue
                await self._handle_message(websocket, meta, raw_message)
        except ConnectionClosed:
            pass
        finally:
            await self._unregister_session(websocket)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signame, self.stop)
            except NotImplementedError:
                break

        if self.advertise_mdns:
            self._mdns_advertiser.start()

        self.logger.info(
            "Starting LAN clipboard daemon",
            extra={
                "event": "daemon.start",
                "mode": "lan",
                "path": f"ws://{self.host}:{self.port}{self.path}",
                "pairing_enabled": str(self.pairing_enabled).lower(),
            },
        )

        poller_task: asyncio.Task[Any] | None = None
        try:
            async with serve(
                self._handler,
                self.host,
                self.port,
                process_request=self._process_request,
                ping_interval=20,
                ping_timeout=20,
                max_size=16 * 1024 * 1024,
            ):
                poller_task = asyncio.create_task(
                    self.poller.run(
                        self._on_local_text_changed,
                        self._on_local_image_changed,
                        self._stop_event,
                    )
                )
                await self._stop_event.wait()
        finally:
            if poller_task is not None:
                poller_task.cancel()
                try:
                    await poller_task
                except asyncio.CancelledError:
                    pass
            self._mdns_advertiser.stop()
            self.logger.info(
                "LAN clipboard daemon stopped",
                extra={"event": "daemon.stop", "mode": "lan"},
            )
