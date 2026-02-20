# Gumlet Port

Umbrella repo for:
1. Mac clipboard bridge code in `mac_bridge/`
2. FlorisBoard fork as a Git submodule in `florisboard/`

This project is the LAN-sync experiment while keeping the ADB workflow as fallback.

Protocol docs:

- `docs/protocol/v1/spec.md`
- `docs/protocol/v1/message.schema.json`
- `docs/mac_bridge_phase2.md`

Pairing:

- One-time QR pairing is available in LAN mode (scan QR -> auto-configure host/port/token).

Run LAN bridge:

```bash
cd Gumlet-board
./.venv/bin/python mac_bridge/sync_clipboard_adb_reference.py --mode=lan
```
