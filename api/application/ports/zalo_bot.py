"""Outbound Zalo Bot delivery contract, independent of provider libraries."""

from __future__ import annotations

from typing import Protocol


class ZaloSendError(RuntimeError):
    """Delivery failed; messages must never contain provider or credential data."""


class ZaloBotSender(Protocol):
    async def send_text(self, chat_id: str, text: str) -> str:
        """Send one bounded plain-text message and return its provider message id."""
        ...
