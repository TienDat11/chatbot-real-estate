"""Vietnamese display labels for internal fact identifiers (round-2 defect D4).

FACT_EVIDENCE carries machine keys (``unit:camellia/2pn-goc``, ``htls``). The
generation LLM has no legend for them, so it echoed the raw keys into
customer-facing prose. This module supplies additive ``*_display`` labels only —
it never replaces the machine keys, which guard_output's numeric grounding and
``[fe-xxx]`` citation checks depend on staying byte-identical.

The DB ``fact_subjects.display_name`` is always preferred when present (it is
curated per project); the slug humanizer here is the fallback for rows whose
subject carries no curated name.
"""

from __future__ import annotations

from typing import Any

# Payment/loan policy keys as spelled in db/seed/*.sql and data/_processed/*.
# 'thanhthoi' is the business_rules.json spelling, 'thanh_thoi' the
# price_matrix.json / facts spelling — both are seeded, so both must resolve.
POLICY_DISPLAY: dict[str, str] = {
    "htls": "Phương án hỗ trợ lãi suất (HTLS)",
    "chuan": "Phương án thanh toán chuẩn",
    "som95": "Phương án thanh toán sớm 95%",
    "thanh_thoi": "Phương án thanh toán thảnh thơi",
    "thanhthoi": "Phương án thanh toán thảnh thơi",
    "bank_a": "Gói vay ngân hàng A",
    "bank_b": "Gói vay ngân hàng B",
    "support": "Chính sách hỗ trợ của chủ đầu tư",
    "project": "Chính sách chung toàn dự án",
}

# Multi-word view/orientation phrases — matched before single tokens so
# 'mat-duong' is never split into 'mat' + 'duong'.
_UNIT_PHRASES: dict[str, str] = {
    "mat-duong": "mặt đường",
    "noi-khu": "nội khu",
    "goc-nui": "góc núi",
    "view-bien": "view biển",
}

# Single unit tokens; keys are the lowercase slug form from subject_key.
_UNIT_TOKENS: dict[str, str] = {
    "studio": "Studio",
    "1pn": "1PN",
    "1p1": "1PN+1",
    "15pn": "1PN+1",
    "2pn": "2PN",
    "3pn": "3PN",
    "4pn": "4PN",
    "goc": "góc",
    "penthouse": "Penthouse",
    "shophouse": "Shophouse",
}

_UNIT_SUBJECT_PREFIX = "unit:"


def policy_display(policy_key: Any) -> str | None:
    """Human label for a policy_key; None when unknown or absent."""
    if not policy_key:
        return None
    return POLICY_DISPLAY.get(str(policy_key).strip().lower())


def humanize_unit_subject_key(subject_key: Any) -> str | None:
    """Turn ``unit:<project>/2pn-mat-duong`` into ``Căn hộ 2PN mặt đường``.

    Returns None for anything this dictionary cannot fully resolve (concrete
    unit codes such as ``CH-03A`` or ``A06-01``), so callers fall back to the
    curated DB display name instead of a half-translated string.
    """
    if not subject_key:
        return None
    raw = str(subject_key).strip()
    if not raw.startswith(_UNIT_SUBJECT_PREFIX) or "/" not in raw:
        return None
    token = raw.rsplit("/", 1)[-1].strip().lower()
    if not token:
        return None
    parts = [p for p in token.split("-") if p]
    if not parts:
        return None

    labels: list[str] = []
    i = 0
    while i < len(parts):
        phrase = "-".join(parts[i : i + 2])
        if phrase in _UNIT_PHRASES:
            labels.append(_UNIT_PHRASES[phrase])
            i += 2
            continue
        if parts[i] in _UNIT_TOKENS:
            labels.append(_UNIT_TOKENS[parts[i]])
            i += 1
            continue
        return None  # unresolved token -> prefer the curated display name
    return f"Căn hộ {' '.join(labels)}"


def subject_display(subject_key: Any, display_name: Any = None) -> str | None:
    """Best customer-facing name for a subject: curated name first, slug second.

    A subject that is already prose (no ``<type>:`` namespace, e.g. the
    affordability leg which ships display_name as ``subject``) is echoed so every
    fe block carries the same legend field; an unresolved machine key returns
    None rather than leaking a raw key into the display slot.
    """
    if display_name:
        return str(display_name).strip()
    if not subject_key:
        return None
    raw = str(subject_key).strip()
    if ":" not in raw:
        return raw or None
    return humanize_unit_subject_key(raw)


def enrich_evidence_entry(entry: dict) -> dict:
    """Add ``subject_display`` / ``policy_display`` to one fe block (additive).

    Applied to every evidence producer's output, so entries that bypass
    build_fact_evidence (affordability, NL2SQL) still carry a legend. Existing
    keys are never touched and no key is removed.
    """
    if not isinstance(entry, dict):
        return entry
    enriched = dict(entry)
    subject = enriched.get("subject")
    display = subject_display(subject, enriched.get("display_name"))
    if display and "subject_display" not in enriched:
        enriched["subject_display"] = display
    policy = enriched.get("policy_key")
    policy_label = policy_display(policy)
    if policy_label and "policy_display" not in enriched:
        enriched["policy_display"] = policy_label
    return enriched
