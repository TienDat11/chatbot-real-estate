"""Single storage normalizer for fact subject keys (E contract).

Persisted semantics (unchanged): strip, lowercase, drop dots/dashes.
Only storage/query representation is normalized; user-facing unit codes stay canonical.
"""

from __future__ import annotations


def normalize_subject_key(key: str) -> str:
    """Normalize a subject key for storage/lookup before the UNIQUE constraint."""
    return key.strip().lower().replace(".", "").replace("-", "")
