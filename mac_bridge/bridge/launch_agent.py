from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Sequence


LAUNCH_AGENT_LABEL = "dev.gumlet.clipboard-bridge"


def launch_agent_plist_path(home: Path | None = None) -> Path:
    base_home = home if home is not None else Path.home()
    return base_home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def launch_agent_domain(uid: int | None = None) -> str:
    resolved_uid = os.getuid() if uid is None else uid
    return f"gui/{resolved_uid}"


def render_launch_agent_plist(
    *,
    python_path: Path,
    entrypoint_path: Path,
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    program_arguments: Sequence[str],
) -> bytes:
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(python_path), str(entrypoint_path), *program_arguments],
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return plistlib.dumps(plist, sort_keys=True)


def install_launch_agent(
    *,
    plist_path: Path,
    plist_bytes: bytes,
    domain: str,
) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plist_bytes)

    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "enable", f"{domain}/{LAUNCH_AGENT_LABEL}"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "kickstart", "-kp", f"{domain}/{LAUNCH_AGENT_LABEL}"],
        check=True,
        capture_output=True,
        text=True,
    )


def uninstall_launch_agent(*, plist_path: Path, domain: str) -> None:
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if plist_path.exists():
        plist_path.unlink()
