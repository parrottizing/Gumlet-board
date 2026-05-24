from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

from bridge.launch_agent import (
    LAUNCH_AGENT_LABEL,
    launch_agent_domain,
    launch_agent_plist_path,
    render_launch_agent_plist,
)


class LaunchAgentTest(unittest.TestCase):
    def test_launch_agent_plist_path_uses_expected_label(self) -> None:
        home = Path("/Users/tester")
        self.assertEqual(
            launch_agent_plist_path(home),
            home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist",
        )

    def test_launch_agent_domain_targets_gui_session(self) -> None:
        self.assertEqual(launch_agent_domain(501), "gui/501")

    def test_render_launch_agent_plist_contains_expected_runtime_config(self) -> None:
        plist_bytes = render_launch_agent_plist(
            python_path=Path("/repo/.venv/bin/python"),
            entrypoint_path=Path("/repo/mac_bridge/sync_clipboard_adb_reference.py"),
            working_directory=Path("/repo"),
            stdout_path=Path("/tmp/gumlet.stdout.log"),
            stderr_path=Path("/tmp/gumlet.stderr.log"),
            program_arguments=(
                "--mode=lan",
                "--state-path",
                "/tmp/state.json",
                "--log-dir",
                "/tmp/logs",
            ),
        )

        payload = plistlib.loads(plist_bytes)
        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/repo/.venv/bin/python",
                "/repo/mac_bridge/sync_clipboard_adb_reference.py",
                "--mode=lan",
                "--state-path",
                "/tmp/state.json",
                "--log-dir",
                "/tmp/logs",
            ],
        )
        self.assertEqual(payload["StandardOutPath"], "/tmp/gumlet.stdout.log")
        self.assertEqual(payload["StandardErrorPath"], "/tmp/gumlet.stderr.log")

if __name__ == "__main__":
    unittest.main()
