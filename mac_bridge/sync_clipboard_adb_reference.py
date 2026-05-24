#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

from bridge.clipboard_io import MacClipboardBackend
from bridge.launch_agent import (
    LAUNCH_AGENT_LABEL,
    install_launch_agent,
    launch_agent_domain,
    launch_agent_plist_path,
    render_launch_agent_plist,
    uninstall_launch_agent,
)
from bridge.logging_utils import configure_logging
from bridge.state_store import StateStore

DEFAULT_RUNTIME_DIR = Path.home() / ".gumlet_clipboard_bridge"
DEFAULT_STATE_PATH = DEFAULT_RUNTIME_DIR / "state.json"
DEFAULT_LOG_DIR = DEFAULT_RUNTIME_DIR / "logs"
DEFAULT_PAIRING_QR_PATH = DEFAULT_RUNTIME_DIR / "pairing_qr.png"
DEFAULT_LAUNCHD_STDOUT_PATH = DEFAULT_LOG_DIR / "launchd.stdout.log"
DEFAULT_LAUNCHD_STDERR_PATH = DEFAULT_LOG_DIR / "launchd.stderr.log"


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
    parser.add_argument(
        "--disable-pairing",
        action="store_true",
        help="Disable one-time QR pairing endpoint and startup pairing offer generation.",
    )
    parser.add_argument(
        "--pairing-host",
        default="",
        help="Optional host value embedded in the pairing deep link. Defaults to detected LAN IP.",
    )
    parser.add_argument(
        "--pairing-code-ttl-seconds",
        type=int,
        default=180,
        help="Lifetime of one-time pairing codes in seconds.",
    )
    parser.add_argument(
        "--pairing-qr-path",
        type=Path,
        default=DEFAULT_PAIRING_QR_PATH,
        help="Path to write startup pairing QR image PNG.",
    )
    parser.add_argument(
        "--install-launch-agent",
        action="store_true",
        help="Install/update a per-user launchd agent for always-on LAN mode.",
    )
    parser.add_argument(
        "--uninstall-launch-agent",
        action="store_true",
        help="Remove the per-user launchd agent for always-on LAN mode.",
    )
    parser.add_argument(
        "--print-launch-agent-plist-path",
        action="store_true",
        help="Print the launchd plist path used for always-on LAN mode and exit.",
    )
    return parser


def run_adb_mode() -> None:
    from bridge.transport_adb import run_adb_sync

    run_adb_sync()


def _print_pairing_qr_ascii(data: str) -> bool:
    try:
        import qrcode
    except ImportError:
        return False

    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    output = io.StringIO()
    qr.print_ascii(out=output)
    output.seek(0)
    print(output.read())
    return True


def _write_pairing_qr_png(data: str, path: Path) -> bool:
    try:
        import qrcode
    except ImportError:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(data)
    img.save(path)
    return True


def _print_pairing_offer(offer, pairing_qr_path: Path) -> None:
    expires_utc = datetime.fromtimestamp(offer.expires_at_ms / 1000, tz=timezone.utc)
    print("\n=== LAN Pairing Offer (one-time) ===")
    print(f"Deep link: {offer.deep_link}")
    print(f"Expires at (UTC): {expires_utc.isoformat()}")
    print("Scan this QR with Android to auto-pair host/port/token.\n")
    if not _print_pairing_qr_ascii(offer.deep_link):
        print("ASCII QR unavailable (install/upgrade python 'qrcode' package).")
    if _write_pairing_qr_png(offer.deep_link, pairing_qr_path):
        print(f"Saved pairing QR PNG: {pairing_qr_path}\n")
    else:
        print("Pairing QR PNG unavailable (install/upgrade python 'qrcode' package).\n")


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
        pairing_enabled=not args.disable_pairing,
        pairing_host=args.pairing_host,
        pairing_code_ttl_seconds=args.pairing_code_ttl_seconds,
    )
    pairing_offer = server.issue_pairing_offer()
    if pairing_offer is not None:
        _print_pairing_offer(pairing_offer, args.pairing_qr_path)
    elif args.disable_pairing:
        print("One-time LAN pairing is disabled (--disable-pairing).")
    asyncio.run(server.run())


def _launch_agent_program_arguments(args: argparse.Namespace) -> list[str]:
    program_args = [
        "--mode=lan",
        "--state-path",
        str(args.state_path),
        "--log-dir",
        str(args.log_dir),
        "--log-level",
        args.log_level,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--path",
        args.path,
        "--service-type",
        args.service_type,
        "--poll-interval",
        str(args.poll_interval),
        "--pairing-code-ttl-seconds",
        str(args.pairing_code_ttl_seconds),
        "--pairing-qr-path",
        str(args.pairing_qr_path),
    ]
    if args.disable_mdns:
        program_args.append("--disable-mdns")
    if args.disable_pairing:
        program_args.append("--disable-pairing")
    if args.pairing_host:
        program_args.extend(["--pairing-host", args.pairing_host])
    return program_args


def handle_launch_agent_command(args: argparse.Namespace) -> bool:
    if not (args.install_launch_agent or args.uninstall_launch_agent or args.print_launch_agent_plist_path):
        return False

    plist_path = launch_agent_plist_path()
    if args.print_launch_agent_plist_path:
        print(plist_path)
        return True

    domain = launch_agent_domain()
    if args.uninstall_launch_agent:
        uninstall_launch_agent(plist_path=plist_path, domain=domain)
        print(f"Removed launch agent {LAUNCH_AGENT_LABEL}")
        return True

    stdout_path = DEFAULT_LAUNCHD_STDOUT_PATH
    stderr_path = DEFAULT_LAUNCHD_STDERR_PATH
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    entrypoint_path = Path(__file__).resolve()
    working_directory = entrypoint_path.parent.parent.resolve()
    plist_bytes = render_launch_agent_plist(
        python_path=Path(sys.executable).resolve(),
        entrypoint_path=entrypoint_path,
        working_directory=working_directory,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        program_arguments=_launch_agent_program_arguments(args),
    )
    install_launch_agent(
        plist_path=plist_path,
        plist_bytes=plist_bytes,
        domain=domain,
    )
    print(f"Installed launch agent {LAUNCH_AGENT_LABEL}")
    print(f"Plist: {plist_path}")
    return True


def main() -> None:
    args = build_parser().parse_args()
    if handle_launch_agent_command(args):
        return

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
