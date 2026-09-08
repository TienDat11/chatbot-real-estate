"""Port for server-side Firebase sales account provisioning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SalesProvisioningResult:
    """Records whether provisioning found an already-active external account."""

    pre_existing_active: bool = False


class SalesProvisioningError(Exception):
    """Raised when Firebase claim/profile provisioning cannot complete."""

    def __init__(self, message: str, *, reconciliation_failed: bool = False) -> None:
        super().__init__(message)
        self.reconciliation_failed = reconciliation_failed


class SalesProvisioner(Protocol):
    async def provision(
        self,
        *,
        firebase_uid: str,
        full_name: str,
        email: str | None = None,
    ) -> SalesProvisioningResult: ...

    async def revoke(self, *, firebase_uid: str) -> None: ...
