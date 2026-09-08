"""Regression tests for the re-embed CLI guards.

Reviewer BLOCKER fix: --source-suffix is CLI input interpolated into PG table
identifiers, so it must be locked to plain [a-z0-9_] fragments before any SQL
is built.
"""

from __future__ import annotations

import pytest

from scripts.reembed_embeddings import _model_suffix, _safe_suffix, parse_args


def test_safe_suffix_accepts_plain_identifier():
    assert _safe_suffix("text_embedding_v4_1024d") == "text_embedding_v4_1024d"


@pytest.mark.parametrize(
    "bad",
    [
        "x; drop table users",
        "default); delete from lightrag_vdb_chunks_x--",
        'x" or "1"="1',
        "with space",
        "UPPER_CASE",
        "",
        "a-b",
    ],
)
def test_safe_suffix_rejects_non_identifier_input(bad: str):
    with pytest.raises(Exception):
        _safe_suffix(bad)


def test_parse_args_rejects_malicious_source_suffix():
    with pytest.raises(SystemExit):
        parse_args(["--source-suffix", "x; drop table users"])


def test_model_suffix_stays_identifier_safe():
    assert _model_suffix("gemini-embedding-001", 1024) == "gemini_embedding_001_1024d"
