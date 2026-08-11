"""Placeholder ⟦FACT:key@subject[#policy]⟧ — ánh xạ chunk ↔ facts (logical ref, không row id).

Plan §3.5: token = logical ref; resolve() trong with_rls_identity; fact hết hiệu lực/không
tồn tại → marker '[không có dữ liệu hiệu lực]' (KHÔNG silent drop). §3.2 step 2: sanitize
forged token TRƯỚC khi thay span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

FACT_TOKEN_START = "⟦FACT:"
FACT_TOKEN_END = "⟧"

# ⟦FACT:price_vnd@unit:tower-a/A10-01#bank_a⟧
_PLACEHOLDER_RE = re.compile(r"⟦FACT:([a-zA-Z0-9_\.\-]+)@([^#⟧]+?)(?:#([a-zA-Z0-9_\.\-]+))?⟧")


@dataclass(frozen=True)
class FactRef:
    fact_key: str
    subject_key: str
    policy_key: str | None = None

    @property
    def token(self) -> str:
        tail = f"#{self.policy_key}" if self.policy_key else ""
        return f"{FACT_TOKEN_START}{self.fact_key}@{self.subject_key}{tail}{FACT_TOKEN_END}"


def sanitize_forged_tokens(text: str) -> str:
    """Escapes literal ⟦ ⟧ có sẵn trong source (plan §3.2 step 2) để không lẫn với token thật."""
    # Thay ⟦ → ⟪ và ⟧ → ⟫ (ký tự U+27EA/27EB) — an toàn, không đụng token ta tạo sau này.
    return text.replace(FACT_TOKEN_START, "⟪FACT:").replace(FACT_TOKEN_END, "⟫")


def replace_fact_with_placeholder(text: str, spans: list[tuple[str, str, str | None]]) -> tuple[str, list[FactRef]]:
    """Thay đoạn gốc (span) bằng token placeholder.

    Args:
        text: chunk text (đã sanitize forged token).
        spans: list (subject_key, fact_key, policy_key).

    Returns:
        (text mới, list FactRef) — refs khớp 1:1 thứ tự span. Thêm prefix token ở đầu chunk
        để tracker + integrity check (verify_ingest.sql) hoạt động đúng.
    """
    refs = [FactRef(subject_key=s, fact_key=fk, policy_key=p) for s, fk, p in spans]
    prefix = "".join(r.token for r in refs)
    new_text = prefix + "\n" + text if prefix else text
    return new_text, refs


def extract_placeholders(text: str) -> list[FactRef]:
    """Đọc token từ text (dùng verify_ingest + hydrate)."""
    return [
        FactRef(fact_key=m.group(1), subject_key=m.group(2), policy_key=m.group(3))
        for m in _PLACEHOLDER_RE.finditer(text)
    ]


def has_dangling_placeholder(text: str) -> bool:
    """Token mở mà không đóng (integrity test A13)."""
    opens = text.count(FACT_TOKEN_START)
    closes = text.count(FACT_TOKEN_END)
    return opens > closes


Resolver = Callable[[str, str, str | None], str | None]  # (fact_key, subject_key, policy_key) -> formatted


def resolve_placeholders(text: str, resolver: Resolver) -> str:
    """Hydrate token → giá trị fact (format sẵn kèm ngày hiệu lực) qua resolver.

    Fact hết hiệu lực / không tồn tại → resolver trả None → marker '[không có dữ liệu hiệu lực]'.
    """

    def _repl(m: re.Match[str]) -> str:
        fact_key, subject_key, policy_key = m.group(1), m.group(2), m.group(3)
        value = resolver(fact_key, subject_key, policy_key)
        if value is None:
            return "[không có dữ liệu hiệu lực]"
        return value

    return _PLACEHOLDER_RE.sub(_repl, text)
