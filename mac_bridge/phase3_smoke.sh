#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRYPOINT="$ROOT_DIR/mac_bridge/sync_clipboard_adb_reference.py"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: Python runtime not found: $PYTHON_BIN" >&2
  exit 1
fi

if ! "$PYTHON_BIN" - <<'PY'
import importlib.util
import sys

required = ("websockets",)
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    print(
        "ERROR: Missing Python dependencies: "
        + ", ".join(missing)
        + ". Install with: python3 -m pip install -r mac_bridge/requirements_base.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
then
  exit 1
fi

RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gumlet-phase3-smoke.XXXXXX")"
STATE_PATH="$RUNTIME_DIR/state.json"
LOG_DIR="$RUNTIME_DIR/logs"
DAEMON_STDOUT="$RUNTIME_DIR/daemon.stdout.log"
LOG_FILE="$LOG_DIR/bridge.log"
KEEP_ARTIFACTS="${KEEP_ARTIFACTS:-0}"
DAEMON_PID=""

cleanup() {
  local status=$?
  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" >/dev/null 2>&1; then
    kill "$DAEMON_PID" >/dev/null 2>&1 || true
    wait "$DAEMON_PID" >/dev/null 2>&1 || true
  fi

  if [[ "$status" -eq 0 && "$KEEP_ARTIFACTS" != "1" ]]; then
    rm -rf "$RUNTIME_DIR"
  else
    echo "Artifacts kept at: $RUNTIME_DIR"
  fi
}
trap cleanup EXIT

PORT="$("$PYTHON_BIN" - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

echo "Starting LAN daemon on 127.0.0.1:$PORT"
"$PYTHON_BIN" "$ENTRYPOINT" \
  --mode=lan \
  --host=127.0.0.1 \
  --port="$PORT" \
  --disable-mdns \
  --log-level=DEBUG \
  --state-path "$STATE_PATH" \
  --log-dir "$LOG_DIR" \
  >"$DAEMON_STDOUT" 2>&1 &
DAEMON_PID="$!"

if ! "$PYTHON_BIN" - "$PORT" <<'PY'
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/healthz"
deadline = time.time() + 8.0
while time.time() < deadline:
    try:
        with urlopen(url, timeout=0.5) as response:
            body = response.read().decode("utf-8").strip()
            if body == "OK":
                raise SystemExit(0)
    except URLError:
        time.sleep(0.1)
time.sleep(0.1)
raise SystemExit(1)
PY
then
  echo "ERROR: healthz check failed. Daemon output: $DAEMON_STDOUT" >&2
  exit 1
fi

echo "Health check passed"

TOKEN="$("$PYTHON_BIN" "$ENTRYPOINT" --state-path "$STATE_PATH" --print-token | tr -d '\n')"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: Failed to read pairing token from state file" >&2
  exit 1
fi

if ! "$PYTHON_BIN" - "$ROOT_DIR" "$PORT" "$TOKEN" <<'PY'
import asyncio
import json
import sys
from pathlib import Path

from websockets.asyncio.client import connect

root_dir = Path(sys.argv[1])
port = int(sys.argv[2])
token = sys.argv[3]
sys.path.insert(0, str(root_dir / "mac_bridge"))

from bridge.event_normalizer import normalize_event  # noqa: E402


async def run() -> None:
    uri = f"ws://127.0.0.1:{port}/v1/clipboard"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Clipboard-Protocol-Version": "1.0",
        "X-Clipboard-Device-Id": "android.phase3.smoke",
        "X-Clipboard-Source": "android",
    }
    async with connect(uri, additional_headers=headers) as ws:
        ping_event = normalize_event(
            device_id="android.phase3.smoke",
            source="android",
            event_type="ping",
            payload={"nonce": "phase3-smoke"},
        )
        await ws.send(json.dumps(ping_event, separators=(",", ":")))
        raw = await asyncio.wait_for(ws.recv(), timeout=3)
        envelope = json.loads(raw)
        if envelope.get("event_type") != "pong":
            raise RuntimeError(f"Expected pong event, got: {envelope.get('event_type')}")


asyncio.run(run())
PY
then
  echo "ERROR: websocket ping/pong exchange failed" >&2
  exit 1
fi

echo "Ping/pong exchange passed"

if ! "$PYTHON_BIN" - "$LOG_FILE" <<'PY'
import json
import sys
import time
from pathlib import Path

log_file = Path(sys.argv[1])
required = {"daemon.start", "session.connected", "session.disconnected"}
deadline = time.time() + 5.0

while time.time() < deadline:
    found = set()
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = payload.get("event")
            if isinstance(event, str):
                found.add(event)
    missing = required - found
    if not missing:
        raise SystemExit(0)
    time.sleep(0.1)

print(f"Missing log events: {sorted(missing)}", file=sys.stderr)
raise SystemExit(1)
PY
then
  echo "ERROR: expected session/daemon events not found in log file: $LOG_FILE" >&2
  exit 1
fi

echo "Log assertions passed"
echo "Phase 3 smoke check passed"
