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

