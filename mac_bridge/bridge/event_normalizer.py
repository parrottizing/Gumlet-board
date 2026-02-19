from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

PROTOCOL_VERSION = "1.0"
STALE_EVENT_WINDOW_MS = 120_000
SUPPORTED_EVENT_TYPES = {"set_text", "set_image", "ack", "error", "ping", "pong"}
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
ALLOWED_IMAGE_ORIENTATIONS = {0, 90, 180, 270}


@dataclass(slots=True)
class ProtocolError(Exception):
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def now_ms() -> int:
    return int(time.time() * 1000)


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = canonical_payload_json(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def normalize_event(
    *,
    device_id: str,
    source: str,
    event_type: str,
    payload: Mapping[str, Any],
    event_id: str | None = None,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    event_payload = dict(payload)
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "device_id": device_id,
        "event_id": event_id or str(uuid.uuid4()),
        "source": source,
        "event_type": event_type,
        "created_at_ms": created_at_ms if created_at_ms is not None else now_ms(),
        "payload_hash": compute_payload_hash(event_payload),
        "payload": event_payload,
    }
    validate_event(envelope)
    return envelope


def make_ack(
    *,
    device_id: str,
    source: str,
    acked_event_id: str,
    status: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"acked_event_id": acked_event_id, "status": status}
    if error_code:
        payload["error_code"] = error_code
    return normalize_event(
        device_id=device_id,
        source=source,
        event_type="ack",
        payload=payload,
    )


def make_error(
    *,
    device_id: str,
    source: str,
    code: str,
    message: str,
    retryable: bool = False,
) -> dict[str, Any]:
    payload = {"code": code, "message": message, "retryable": retryable}
    return normalize_event(
        device_id=device_id,
        source=source,
        event_type="error",
        payload=payload,
    )


def parse_json_message(raw_message: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise ProtocolError("BAD_MESSAGE", "Payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("BAD_MESSAGE", "Top-level message must be a JSON object")
    return parsed


def _validate_device_id(device_id: Any) -> None:
    if not isinstance(device_id, str) or not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ProtocolError("BAD_MESSAGE", "Invalid device_id format")


def _validate_version(version: Any) -> None:
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version):
        raise ProtocolError("BAD_MESSAGE", "protocol_version must use major.minor")
    major, _minor = version.split(".", maxsplit=1)
    if major != PROTOCOL_VERSION.split(".", maxsplit=1)[0]:
        raise ProtocolError("VERSION_MISMATCH", "Unsupported protocol major version")


def _validate_base(envelope: Mapping[str, Any]) -> None:
    required = (
        "protocol_version",
        "device_id",
        "event_id",
        "source",
        "event_type",
        "created_at_ms",
        "payload_hash",
        "payload",
    )
    for key in required:
        if key not in envelope:
            raise ProtocolError("BAD_MESSAGE", f"Missing required field: {key}")
    _validate_version(envelope["protocol_version"])
    _validate_device_id(envelope["device_id"])
    if not isinstance(envelope["event_id"], str) or len(envelope["event_id"]) < 8:
        raise ProtocolError("BAD_MESSAGE", "Invalid event_id")
    if envelope["event_type"] not in SUPPORTED_EVENT_TYPES:
        raise ProtocolError("UNSUPPORTED_TYPE", "Unsupported event_type")
    if not isinstance(envelope["created_at_ms"], int):
        raise ProtocolError("BAD_MESSAGE", "created_at_ms must be an integer")
    if not isinstance(envelope["payload"], Mapping):
        raise ProtocolError("BAD_MESSAGE", "payload must be an object")
    if not isinstance(envelope["payload_hash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", envelope["payload_hash"]
    ):
        raise ProtocolError("BAD_MESSAGE", "payload_hash must be lowercase SHA-256 hex")


def _validate_stale_event(envelope: Mapping[str, Any], received_at_ms: int) -> None:
    if envelope["event_type"] not in {"set_text", "set_image"}:
        return
    drift = abs(received_at_ms - envelope["created_at_ms"])
    if drift > STALE_EVENT_WINDOW_MS:
        raise ProtocolError("STALE_EVENT", "created_at_ms exceeds stale window")


def _validate_payload_for_type(envelope: Mapping[str, Any]) -> None:
    event_type = envelope["event_type"]
    payload = envelope["payload"]

    if event_type == "set_text":
        if payload.get("mime_type") != "text/plain":
            raise ProtocolError("UNSUPPORTED_TYPE", "set_text payload mime_type must be text/plain")
        if not isinstance(payload.get("text"), str):
            raise ProtocolError("BAD_MESSAGE", "set_text payload text must be a string")
        if "is_sensitive" in payload and not isinstance(payload["is_sensitive"], bool):
            raise ProtocolError("BAD_MESSAGE", "set_text is_sensitive must be boolean")
    elif event_type == "set_image":
        mime_type = payload.get("mime_type")
        if not isinstance(mime_type, str):
            raise ProtocolError("BAD_MESSAGE", "set_image payload mime_type must be a string")
        normalized_mime_type = mime_type.strip().lower()
        if normalized_mime_type == "image/jpg":
            normalized_mime_type = "image/jpeg"
        if normalized_mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise ProtocolError("UNSUPPORTED_TYPE", "set_image payload mime_type is unsupported")

        byte_size = payload.get("byte_size")
        if not isinstance(byte_size, int) or byte_size <= 0:
            raise ProtocolError("BAD_MESSAGE", "set_image payload byte_size must be a positive integer")
        if byte_size > MAX_IMAGE_BYTES:
            raise ProtocolError("PAYLOAD_TOO_LARGE", "set_image payload exceeds max byte_size")

        data_base64 = payload.get("data_base64")
        if not isinstance(data_base64, str) or not data_base64:
            raise ProtocolError("BAD_MESSAGE", "set_image payload data_base64 must be a non-empty string")
        try:
            decoded = base64.b64decode(data_base64, validate=True)
        except Exception as exc:
            raise ProtocolError("BAD_MESSAGE", "set_image payload data_base64 is invalid") from exc
        if len(decoded) != byte_size:
            raise ProtocolError(
                "BAD_MESSAGE",
                "set_image payload byte_size does not match decoded length",
            )
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ProtocolError("PAYLOAD_TOO_LARGE", "set_image payload exceeds max decoded size")

        width = payload.get("width")
        height = payload.get("height")
        if not isinstance(width, int) or width <= 0:
            raise ProtocolError("BAD_MESSAGE", "set_image payload width must be a positive integer")
        if not isinstance(height, int) or height <= 0:
            raise ProtocolError("BAD_MESSAGE", "set_image payload height must be a positive integer")

        orientation = payload.get("orientation")
        if not isinstance(orientation, int) or orientation not in ALLOWED_IMAGE_ORIENTATIONS:
            raise ProtocolError("BAD_MESSAGE", "set_image payload orientation is invalid")
    elif event_type == "ack":
        status = payload.get("status")
        if status not in {"accepted", "duplicate", "rejected"}:
            raise ProtocolError("BAD_MESSAGE", "ack status is invalid")
        if not isinstance(payload.get("acked_event_id"), str):
            raise ProtocolError("BAD_MESSAGE", "ack requires acked_event_id")
    elif event_type == "error":
        if not isinstance(payload.get("code"), str):
            raise ProtocolError("BAD_MESSAGE", "error requires code")
        if not isinstance(payload.get("message"), str):
            raise ProtocolError("BAD_MESSAGE", "error requires message")
        if "retryable" in payload and not isinstance(payload["retryable"], bool):
            raise ProtocolError("BAD_MESSAGE", "error retryable must be boolean")


def validate_event(envelope: Mapping[str, Any], received_at_ms: int | None = None) -> None:
    _validate_base(envelope)
    _validate_payload_for_type(envelope)
    calculated_hash = compute_payload_hash(envelope["payload"])
    if envelope["payload_hash"] != calculated_hash:
        raise ProtocolError("HASH_MISMATCH", "payload_hash does not match payload")
    _validate_stale_event(envelope, received_at_ms if received_at_ms is not None else now_ms())
