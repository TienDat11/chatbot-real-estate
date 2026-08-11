"""api package — 8-step query pipeline (guard, rewrite, legs, merge, generate, output guard).

Modules are plain functions / testable classes; `workflow.py` orchestrates them.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from api.utils import sha256_hex  # noqa: F401  (re-exported for back-compat)

logger = logging.getLogger("api")


@lru_cache(maxsize=1)
def get_settings() -> object:
    """Return the app Settings singleton (lazy so api imports before config exists)."""
    try:
        from api.config import settings  # noqa: PLC0415  (primary env source)
    except Exception:  # noqa: BLE001
        from ingest.config import settings  # noqa: PLC0415  (legacy re-export)
    return settings


def get_cfg(name: str, default=None):
    """Read a Settings field with a fallback (degrades to default when unavailable)."""
    try:
        s = get_settings()
    except Exception:  # noqa: BLE001 — settings unavailable -> default, not crash
        logger.warning("settings unavailable — defaulting '%s'", name)
        return default
    return getattr(s, name, default)
