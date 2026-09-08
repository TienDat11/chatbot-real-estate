"""ISSUE-G4-01 — vector parity verifier tests (read-only, diagnostic only)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "verify_vector_parity.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_vector_parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses under `from __future__ import annotations` resolve names via
    # sys.modules[cls.__module__], so the module must be registered pre-exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def _result(name="p", expected=162, dims=(1024,), expected_dim=1024, actual=162):
    return verifier.PopulationResult(
        name=name,
        table=f"public.{name}",
        expected_count=expected,
        actual_count=actual,
        expected_dim=expected_dim,
        dims=dims,
        count_ok=actual is not None and actual == expected,
        dims_ok=all(d == expected_dim for d in dims),
    )


# --- expectations resolution (CLI > env > locked 162 baseline) ---

def test_resolve_counts_cli_single_broadcast():
    assert verifier.resolve_expected_counts(162, None, None) == [162, 162, 162, 162]


def test_resolve_counts_cli_list():
    assert verifier.resolve_expected_counts(None, "162,162,162,162", None) == [162] * 4
    assert verifier.resolve_expected_counts(None, "162", None) == [162] * 4


def test_resolve_counts_env_fallback_and_precedence():
    assert verifier.resolve_expected_counts(None, None, "10,10,10,10") == [10] * 4
    assert verifier.resolve_expected_counts(None, "5,5,5,5", "10,10,10,10") == [5] * 4
    assert verifier.resolve_expected_counts(None, None, None) == [162] * 4


def test_resolve_counts_invalid_raises():
    with pytest.raises(ValueError):
        verifier._parse_counts("a,b,c,d")
    with pytest.raises(ValueError):
        verifier._parse_counts("")
    with pytest.raises(ValueError):
        verifier._parse_counts("1,2")


def test_four_populations_match_spec():
    names = [p[0] for p in verifier.VECTOR_POPULATIONS]
    assert names == [
        "registry_document_chunks",
        "lightrag_doc_status",
        "lightrag_doc_chunks",
        "lightrag_vdb_chunks",
    ]
    # Every population query is parameterized by the project filter: the four
    # locked live populations are the Soleil-scoped ones (162/162/162/162).
    assert all("%s" in p[2] for p in verifier.VECTOR_POPULATIONS)


# --- pass/fail semantics (diagnostic only, never repairs) ---

def test_result_pass_and_fail():
    assert _result().passed
    assert not _result(actual=161).passed  # count mismatch
    assert not _result(dims=(1536,)).passed  # dimension mismatch
    assert not _result(actual=None).passed  # missing table
    assert _result(dims=()).passed  # population without vectors: count-only


def test_missing_table_is_fail_not_error():
    res = _result(actual=None)
    out = res.format()
    assert "[FAIL]" in out and "missing" in out


def test_verifier_is_read_only_and_repair_free():
    """Executable strings in the tool must contain no mutation SQL (spec: diagnostic only)."""
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    def docstring_ids(node):
        ids = set()
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            ids.add(id(body[0].value))
        return ids

    skip = docstring_ids(tree)
    for sub in ast.walk(tree):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            skip |= docstring_ids(sub)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in skip:
            continue
        low = " ".join(n.value.lower().split())
        for token in ("insert ", "update ", "delete ", "truncate", "drop ", "alter "):
            assert token not in low, f"mutation token {token!r} in executable string {n.value!r}"


# --- live run against the configured database (skip when unavailable) ---


def _connect(psycopg2):
    from api.infrastructure.config.config import get_settings

    s = get_settings()
    conn = psycopg2.connect(
        host=s.postgres_host, port=s.postgres_port, user=s.postgres_user,
        password=s.postgres_password, dbname=s.postgres_database, connect_timeout=5,
    )
    return conn


def test_live_vector_dimensions_are_1024():
    pytest.importorskip("psycopg2")
    import psycopg2

    try:
        conn = _connect(psycopg2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable for live parity check: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
            if not cur.fetchone():
                pytest.skip("pgvector extension not installed in this database")
            exists = [
                p for p in verifier.VECTOR_POPULATIONS
                if _table_present(cur, p[1])
            ]
            if not exists:
                pytest.skip("no configured vector populations present in this database")
            results = verifier.verify_populations(conn, [162] * 4, 1024)
            present = [r for r in results if r.actual_count is not None]
            assert present, "expected at least one resolvable population"
            for r in present:
                if r.dims:
                    assert set(r.dims) == {1024}, f"{r.name} has non-1024 dims {r.dims}"
    finally:
        conn.close()


def _table_present(cur, qualified: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,))
    return bool(cur.fetchone()[0])


def test_verify_populations_reports_missing_table_as_fail():
    pytest.importorskip("psycopg2")
    import psycopg2

    try:
        conn = _connect(psycopg2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    try:
        bogus_name = f"missing_pop_{uuid.uuid4().hex[:6]}"
        saved = verifier.VECTOR_POPULATIONS
        try:
            verifier.VECTOR_POPULATIONS = (
                (
                    bogus_name,
                    f"public.{bogus_name}",
                    f"SELECT count(*) FROM public.{bogus_name} WHERE id ILIKE %s",
                ),
            )
            results = verifier.verify_populations(conn, [1], 1024)
        finally:
            verifier.VECTOR_POPULATIONS = saved
        assert len(results) == 1
        assert not results[0].passed and results[0].actual_count is None
    finally:
        conn.close()
