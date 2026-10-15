"""Small Zalo transport contracts, independent of provider libraries."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

# Core ProjectResolutionStatus values, source-event replay fallback, transport brake.
ZALO_OUTCOME_STATUSES = frozenset({"resolved", "unresolved", "ambiguous", "replayed", "technical_brake"})


class ZaloAdmissionDecision(Enum):
    STARTED = "started"
    BRAKED = "braked"
    ALREADY_STARTED = "already_started"
    ALREADY_BRAKED = "already_braked"
    LOST_CLAIM = "lost_claim"


class ZaloAdmissionUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Zalo Bot admission unavailable")


class ZaloSendError(RuntimeError):
    """Only fixed codes cross the provider boundary; no payload or URL."""

    def __init__(self, *, code: str = "SEND_UNAVAILABLE", retryable: bool = True) -> None:
        self.code = code if code in {
            "INVALID_TOKEN", "INVALID_RECIPIENT", "INVALID_TEXT",
            "PROVIDER_AUTH_REJECTED", "PROVIDER_REJECTED", "SEND_UNAVAILABLE",
        } else "SEND_UNAVAILABLE"
        self.retryable = retryable if self.code == code else True
        super().__init__("Zalo Bot delivery failed")


class ZaloBotSender(Protocol):
    async def send_text(self, chat_id: str, text: str) -> str:
        """Send one bounded plain-text message and return its provider message id."""
        ...
