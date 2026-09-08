"""VALIDATE the pending chat_sessions customer-identity constraint (G3-r6 §10.3).

The 2026-08-28 unified-sales-workspace migration adds
``chk_sessions_customer_identity`` NOT VALID (expand-and-contract: adding the
check must never fail on legacy rows). This operator script performs the
contract half: it counts violating CUSTOMER rows first and only runs
``VALIDATE CONSTRAINT`` when none exist — violations are reported and left
untouched (a data-review task, never an automatic repair), and training rows
are excluded from the predicate by design.

Exit codes: 0 = validated (or already valid), 2 = violations pending,
1 = infrastructure error. Usage (env POSTGRES_* / build_dsn conventions):

    python scripts/validate_chat_sessions_identity_constraint.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.application.services.sql_leg import build_dsn  # noqa: E402


async def validate_constraint() -> int:
    # The repository method owns the safety sequence: probe pg_constraint,
    # count offenders, VALIDATE only when the count is zero. Imported lazily
    # so the script still prints a clear error when the package is absent.
    from api.infrastructure.adapters.postgres_chat_history import repository

    print(f"target database: {build_dsn().split('@')[-1]}")
    outcome = await repository.validate_customer_identity_constraint()
    if outcome == "validated":
        print("PASS: chk_sessions_customer_identity is valid")
        return 0
    if outcome == "absent":
        print("FAIL: chk_sessions_customer_identity does not exist in this schema")
        print("Remediation: apply db/migrations/2026-08-28-unified-sales-workspace.sql")
        return 1
    # "pending:<n>" — violations found; nothing was written or deleted.
    count = int(outcome.split(":", 1)[1])
    print(f"PENDING: {count} customer rows violate chk_sessions_customer_identity")
    print("Remediation: backfill identity_key/device_id on those rows, then re-run.")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(validate_constraint()))
