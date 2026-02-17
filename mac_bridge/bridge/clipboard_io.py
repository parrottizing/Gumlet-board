from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


class ClipboardBackend(Protocol):
    def get_text(self) -> str:
        ...

    def set_text(self, text: str) -> None:
        ...


class MacClipboardBackend:
    """Mac clipboard backend using pyperclip."""

    def get_text(self) -> str:
        import pyperclip

        value = pyperclip.paste()
        if value is None:
            return ""
        return str(value)

    def set_text(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text)


@dataclass
class InMemoryClipboardBackend:
    """Clipboard backend for tests/simulation."""

    text: str = ""

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text


class ClipboardPoller:
    """Polls text clipboard and calls callback on change."""

    def __init__(self, backend: ClipboardBackend, poll_interval: float = 0.4, logger=None):
        self.backend = backend
        self.poll_interval = poll_interval
        self.logger = logger
        self._last_text: str | None = None

    async def run(
        self,
        on_change: Callable[[str], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                text = self.backend.get_text()
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(
                        "Clipboard read failed",
                        extra={"event": "clipboard.read.error"},
                        exc_info=exc,
                    )
                await asyncio.sleep(self.poll_interval)
                continue

            if self._last_text is None:
                self._last_text = text
            elif text != self._last_text:
                self._last_text = text
                await on_change(text)

            await asyncio.sleep(self.poll_interval)

