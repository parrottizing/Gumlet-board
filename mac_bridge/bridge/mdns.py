from __future__ import annotations

import shutil
import subprocess
from typing import Mapping


class MdnsAdvertiser:
    """
    Thin wrapper over macOS dns-sd for mDNS service advertisement.

    This keeps Phase 2 dependency-light while still publishing a stable service
    identity on LAN.
    """

    def __init__(
        self,
        *,
        service_name: str,
        service_type: str,
        port: int,
        txt_records: Mapping[str, str] | None = None,
        domain: str = "local",
        logger=None,
    ) -> None:
        self.service_name = service_name
        self.service_type = service_type
        self.port = port
        self.domain = domain
        self.txt_records = dict(txt_records or {})
        self.logger = logger
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> bool:
        if self._proc is not None:
            return True

        dns_sd = shutil.which("dns-sd")
        if dns_sd is None:
            if self.logger is not None:
                self.logger.warning(
                    "dns-sd binary not found; mDNS advertisement disabled",
                    extra={"event": "mdns.disabled"},
                )
            return False

        txt_args = [f"{k}={v}" for k, v in sorted(self.txt_records.items())]
        cmd = [
            dns_sd,
            "-R",
            self.service_name,
            self.service_type,
            self.domain,
            str(self.port),
            *txt_args,
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if self.logger is not None:
            self.logger.info(
                "Started mDNS advertisement",
                extra={
                    "event": "mdns.start",
                    "path": f"{self.service_name}.{self.service_type}.{self.domain}",
                },
            )
        return True

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        finally:
            self._proc = None
        if self.logger is not None:
            self.logger.info("Stopped mDNS advertisement", extra={"event": "mdns.stop"})

