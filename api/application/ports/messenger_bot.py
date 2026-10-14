"""Small Messenger transport contracts, independent of provider libraries."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

# Core ProjectResolutionStatus values, source-event replay fallback, transport brake.
MESSENGER_OUTCOME_STATUSES = frozenset({"resolved", "unresolved", "ambiguous", "replayed", "technical_brake"})


class MessengerAdmissionDecision(Enum):
    STARTED = "started"
    BRAKED = "braked"
    ALREADY_STARTED = "already_started"
    ALREADY_BRAKED = "already_braked"
    LOST_CLAIM = "lost_claim"


class MessengerAdmissionUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Messenger Bot admission unavailable")


class MessengerSendError(RuntimeError):
    """Only fixed codes cross the provider boundary; no payload or URL."""

    def __init__(self, *, code: str = "SEND_UNAVAILABLE", retryable: bool = True) -> None:
        self.code = code if code in {
            "INVALID_TOKEN", "INVALID_RECIPIENT", "INVALID_TEXT",
            "PROVIDER_AUTH_REJECTED", "PROVIDER_REJECTED", "SEND_UNAVAILABLE",
            "INVALID_MEDIA",
        } else "SEND_UNAVAILABLE"
        self.retryable = retryable if self.code == code else True
        super().__init__("Messenger Bot delivery failed")


class MessengerBotSender(Protocol):
    async def send_text(self, psid: str, text: str) -> str:
        """Send one bounded plain-text message and return its provider message id."""
        ...

    async def send_quick_replies(self, psid: str, text: str, options: list[tuple[str, str]]) -> str:
        """Send text with tappable quick replies; each option is (title, payload)."""
        ...

    async def send_image(self, psid: str, url: str) -> str:
        """Send an image attachment and return its provider message id."""
        ...
