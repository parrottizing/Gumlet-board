# Mac Bridge Phase 2 Runtime

Phase 2 refactors the old single-file ADB reference into runtime modules and adds a LAN daemon mode.

## Runtime Modes

Use the same entrypoint for both modes:

```bash
python3 mac_bridge/sync_clipboard_adb_reference.py --mode=adb
python3 mac_bridge/sync_clipboard_adb_reference.py --mode=lan
```

- `--mode=adb`: existing ADB fallback runtime.
- `--mode=lan`: WebSocket LAN daemon (`ws://<host>:<port>/v1/clipboard`).

## LAN Daemon Defaults

- Host: `0.0.0.0`
- Port: `8765`
- Path: `/v1/clipboard`
- State file: `~/.gumlet_clipboard_bridge/state.json`
- Logs: `~/.gumlet_clipboard_bridge/logs/bridge.log` (rotating JSON logs)
- mDNS service type: `_gumlet-clipboard._tcp`
- Service identity: persisted in state store and reused across restarts

Print the current token used by LAN auth:

```bash
python3 mac_bridge/sync_clipboard_adb_reference.py --print-token
```

Set/override token:

```bash
python3 mac_bridge/sync_clipboard_adb_reference.py --mode=lan --token "<new-token>"
```

## One-Time QR Pairing (v1 upgrade)

LAN mode now emits a one-time pairing offer on startup:

- a deep link: `ui://florisboard/settings/clipboard?...`
- terminal ASCII QR (when `qrcode` is installed)
- PNG QR at `~/.gumlet_clipboard_bridge/pairing_qr.png`

The QR contains host/port + short-lived pairing code only (no long-lived token).
Floris redeems the code once at `/v1/pair/redeem`, receives token/config, saves it,
and reconnects automatically.

Useful flags:

```bash
python3 mac_bridge/sync_clipboard_adb_reference.py --mode=lan \
  --pairing-code-ttl-seconds 180 \
  --pairing-qr-path ~/.gumlet_clipboard_bridge/pairing_qr.png
```

Disable one-time pairing if needed:

```bash
python3 mac_bridge/sync_clipboard_adb_reference.py --mode=lan --disable-pairing
```

## macOS Autostart

Install a per-user launchd agent so the LAN bridge stays available after login:

```bash
./.venv/bin/python mac_bridge/sync_clipboard_adb_reference.py --install-launch-agent
```

Remove it:

```bash
./.venv/bin/python mac_bridge/sync_clipboard_adb_reference.py --uninstall-launch-agent
```

## Simulated Client Exit Check

Install dependencies:

```bash
python3 -m pip install -r mac_bridge/requirements_base.txt
```

Run tests including reconnect simulation:

```bash
python3 -m unittest discover -s mac_bridge/tests -p "test_*.py" -v
```

`test_lan_runtime.py` starts the LAN daemon, sends text from a simulated client, disconnects, reconnects, and verifies sync still succeeds.

## Phase 3 One-Command Smoke Check

Run from repo root:

```bash
./mac_bridge/phase3_smoke.sh
```

What it verifies:
- starts LAN daemon in a temporary runtime dir
- validates `/healthz`
- performs a real websocket connect + `ping` -> `pong`
- asserts daemon JSON logs contain `daemon.start`, `session.connected`, `session.disconnected`

Optional: keep artifacts/logs after run:

```bash
KEEP_ARTIFACTS=1 ./mac_bridge/phase3_smoke.sh
```
