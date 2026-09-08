"""Shared opt-in test fixtures for staff-gated / training-mode API tests.

Fixtures defined here (re-exported from ``tests._auth_seams``) are the single
source of the offline Firebase-auth seam: a locally signed RSA JWKS verifier
plus a fake PG sales mapping. Tests opt in by naming the fixture; nothing is
autouse, so DB-free tests that never touch staff auth pay zero setup cost.
"""

collect_ignore_glob = ["e2e/*"]
# ``tests/e2e`` runs against live FE+BE dev servers (``pytest -m e2e``) and
# needs ``playwright`` from the repo venv; CI and the default local run use
# ``-m "not e2e"``, so collection must skip the directory entirely — importing
# those modules without playwright installed crashes collection.

import asyncio

import pytest

from tests._auth_seams import (
    foreign_rsa_private_key,  # noqa: F401 — fixture re-export for pytest
    local_rsa_jwk,  # noqa: F401
    offline_auth_seams,  # noqa: F401
)


@pytest.fixture(autouse=True, scope="module")
def _close_cached_pools_after_module() -> None:  # noqa: PT004
    """Release all cached asyncpg pools at module boundaries.

    Supavisor session-mode caps at 15 clients.  Earlier test modules open
    pools (lead, audit, nl2sql, ro, project-registry) that accumulate
    connections; without teardown they consume the session budget by the time
    late-running tests (e.g. the quota race test) execute.  Closing every
    cached pool after each module keeps the budget free for the next module.
    """
    yield
    for closer in (
        "api.infrastructure.adapters.postgres_leads.close_lead_pool",
        "api.application.services.audit.close_audit_pool",
        "api.domain.services.nl2sql_guard.close_nl2sql_pool",
        "api.application.services.sql_leg.close_ro_pool",
    ):
        mod_path, _, fn_name = closer.rpartition(".")
        try:
            mod = __import__(mod_path, fromlist=[fn_name])
            asyncio.run(getattr(mod, fn_name)())
        except Exception:  # noqa: BLE001 — best-effort, never fail teardown
            pass
    # Project-registry pool lives on a lazily-initialised singleton.
    try:
        from api.infrastructure.dependencies import get_project_registry  # noqa: PLC0415

        registry = get_project_registry()
        close = getattr(registry, "close", None)
        if close is not None:
            asyncio.run(close())
    except Exception:  # noqa: BLE001
        pass
