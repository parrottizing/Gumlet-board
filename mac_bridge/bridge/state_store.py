from __future__ import annotations

import copy
import json
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


class StateStore:
    """Persists pairing token + per-device sync state."""

    CURRENT_VERSION = 1

    def __init__(self, path: Path, logger=None) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.load()

    @classmethod
    def default_state(cls) -> dict[str, Any]:
        return {
            "version": cls.CURRENT_VERSION,
            "token": "",
            "service_identity": "",
            "paired_devices": {},
            "device_sync_state": {},
        }

    def _log(self, level: str, message: str, **extra: Any) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level.lower())(message, extra=extra)

    def load(self) -> dict[str, Any]:
        with self._lock:
            raw = self.default_state()
            if self.path.exists():
                try:
                    raw.update(json.loads(self.path.read_text(encoding="utf-8")))
                except Exception:
                    self._log("warning", "Failed to parse state file; recreating")

            if not raw.get("token"):
                raw["token"] = secrets.token_urlsafe(32)
            if not raw.get("service_identity"):
                hostname = socket.gethostname().replace(" ", "-").lower()
                raw["service_identity"] = (
                    f"gumlet-bridge-{hostname}-{secrets.token_hex(3)}"
                )

            self._data = raw
            self._persist_locked()
            return copy.deepcopy(self._data)

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
        ) as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
            handle.flush()
            tmp_name = handle.name
        Path(tmp_name).replace(self.path)

    def save(self) -> None:
        with self._lock:
            self._persist_locked()

    @property
    def token(self) -> str:
        with self._lock:
            return str(self._data["token"])

    @property
    def service_identity(self) -> str:
        with self._lock:
            return str(self._data["service_identity"])

    def set_token(self, token: str) -> None:
        with self._lock:
            self._data["token"] = token
            self._persist_locked()

    def paired_devices(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data["paired_devices"])

    def record_paired_device(self, device_id: str, source: str) -> None:
        now_ms = _now_ms()
        with self._lock:
            paired_devices = self._data.setdefault("paired_devices", {})
            current = paired_devices.get(device_id)
            if current is None:
                paired_devices[device_id] = {
                    "source": source,
                    "first_seen_ms": now_ms,
                    "last_seen_ms": now_ms,
                }
            else:
                current["source"] = source
                current["last_seen_ms"] = now_ms
            self._persist_locked()

    def get_device_sync_state(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._data.get("device_sync_state", {}).get(device_id, {})
            return copy.deepcopy(state)

    def update_device_sync_state(
        self,
        device_id: str,
        *,
        last_event_id: str,
        last_payload_hash: str,
    ) -> None:
        now_ms = _now_ms()
        with self._lock:
            sync_state = self._data.setdefault("device_sync_state", {})
            sync_state[device_id] = {
                "last_event_id": last_event_id,
                "last_payload_hash": last_payload_hash,
                "updated_at_ms": now_ms,
            }
            self._persist_locked()

