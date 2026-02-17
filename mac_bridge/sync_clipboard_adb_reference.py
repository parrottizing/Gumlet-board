#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from bridge.clipboard_io import MacClipboardBackend
from bridge.logging_utils import configure_logging
from bridge.state_store import StateStore

DEFAULT_RUNTIME_DIR = Path.home() / ".gumlet_clipboard_bridge"
DEFAULT_STATE_PATH = DEFAULT_RUNTIME_DIR / "state.json"
DEFAULT_LOG_DIR = DEFAULT_RUNTIME_DIR / "logs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlorisBoard macOS bridge runtime with ADB fallback and LAN daemon mode."
    )
    parser.add_argument(
        "--mode",
        choices=("adb", "lan"),
        default="adb",
        help="Runtime transport mode.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to local daemon state store JSON file.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for structured rotating daemon logs.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print active pairing token from local state and exit.",
    )

    parser.add_argument("--host", default="0.0.0.0", help="LAN daemon bind host.")
    parser.add_argument("--port", default=8765, type=int, help="LAN daemon bind port.")
    parser.add_argument(
        "--path",
        default="/v1/clipboard",
        help="WebSocket path for LAN clipboard protocol.",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Override and persist the pairing token for LAN mode.",
    )
    parser.add_argument(
        "--service-type",
        default="_gumlet-clipboard._tcp",
        help="mDNS service type to advertise in LAN mode.",
    )
    parser.add_argument(
        "--disable-mdns",
        action="store_true",
        help="Disable mDNS advertisement in LAN mode.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.4,
        help="Clipboard poll interval in seconds for LAN mode.",
    )
    return parser


def run_adb_mode() -> None:
    from bridge.transport_adb import run_adb_sync

    run_adb_sync()


def run_lan_mode(args: argparse.Namespace, state_store: StateStore, logger) -> None:
    try:
        from bridge.transport_lan import LanClipboardServer
    except ImportError as exc:
        raise SystemExit(
            "LAN mode requires optional dependencies. Install with: "
            "python3 -m pip install -r mac_bridge/requirements_base.txt"
        ) from exc

    clipboard = MacClipboardBackend()
    server = LanClipboardServer(
        host=args.host,
        port=args.port,
        path=args.path,
        state_store=state_store,
        clipboard=clipboard,
        logger=logger,
        advertise_mdns=not args.disable_mdns,
        mdns_service_type=args.service_type,
        poll_interval=args.poll_interval,
    )
    asyncio.run(server.run())


def main() -> None:
    args = build_parser().parse_args()
    logger = configure_logging("gumlet.mac_bridge", args.log_dir, level=args.log_level)
    state_store = StateStore(args.state_path, logger=logger)

    if args.token:
        state_store.set_token(args.token.strip())

    if args.print_token:
        print(state_store.token)
        return

    if args.mode == "adb":
        logger.info("Starting ADB fallback runtime", extra={"event": "daemon.start", "mode": "adb"})
        run_adb_mode()
        return

    run_lan_mode(args, state_store, logger)


if __name__ == "__main__":
    main()
