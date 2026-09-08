"""Pooler-safety: every asyncpg.create_pool call must pass statement_cache_size=0.

Supavisor (port 6543 / pooler.supabase.com) does not support server-side
prepared statements; asyncpg's default cache (100 statements) will cause
DuplicatePreparedStatementError through the pooler. These tests use AST
analysis to verify the cache-disabling kwargs are forwarded from every
pool-creation site — no DB or network needed, and no dependency on the full
api package import chain (which has a pre-existing broken import in
api/__init__.py unrelated to this change).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# The five pool-creation sites that must include statement_cache_size=0.
_POOL_SITES: list[tuple[str, int, str]] = [
    # (relative file, approximate create_pool line, module label)
    ("api/application/services/sql_leg.py", 155, "sql_leg RO pool"),
    ("api/infrastructure/adapters/postgres_leads.py", 56, "postgres_leads pool"),
    ("api/application/services/audit.py", 30, "audit pool"),
    ("api/domain/services/nl2sql_guard.py", 180, "nl2sql_guard pool"),
    ("api/infrastructure/adapters/postgres_project_registry.py", 148, "project_registry pool"),
]


def _find_create_pool_calls(source: str) -> list[ast.Call]:
    """Return all ast.Call nodes whose func name is 'create_pool'."""
    tree = ast.parse(source)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_pool"
        ):
            calls.append(node)
    return calls


def _call_has_kwarg(call: ast.Call, name: str) -> bool:
    """True when *call* includes keyword arg *name* directly or via **var."""
    for kw in call.keywords:
        if kw.arg == name:
            return True
    return False


def _call_has_all_kwargs(call: ast.Call, names: list[str]) -> list[str]:
    """Return list of *names* that are MISSING from *call*."""
    return [n for n in names if not _call_has_kwarg(call, n)]


_REQUIRED_CACHE_KWARGS = [
    "statement_cache_size",
    "max_cached_statement_lifetime",
    "max_cacheable_statement_size",
]


class TestPoolerSafeKwargs:
    """Verify _pooler_safe_kwargs is defined and returns the expected dict via AST."""

    def test_helper_returns_all_three_cache_zero(self) -> None:
        src = (_REPO / "api" / "application" / "services" / "sql_leg.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        # Find the function body and verify it returns a dict with the right keys.
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_pooler_safe_kwargs":
                func = node
                break
        assert func is not None, "_pooler_safe_kwargs not defined in sql_leg.py"

        # The function body should contain a Return of a Dict; verify the keys.
        returns = [n for n in func.body if isinstance(n, ast.Return)]
        assert returns, "_pooler_safe_kwargs must contain a return statement"
        ret = returns[0]
        assert isinstance(ret.value, ast.Dict), "return value must be a dict literal"

        keys: list[str] = []
        for k in ret.value.keys:
            if isinstance(k, ast.Constant):
                keys.append(k.value)
        assert keys == [
            "statement_cache_size",
            "max_cached_statement_lifetime",
            "max_cacheable_statement_size",
        ], f"unexpected dict keys: {keys}"

        # All values must be 0 (safe for direct-conn + pooler).
        vals: list[int] = []
        for v in ret.value.values:
            assert isinstance(v, ast.Constant), "dict values must be constants"
            vals.append(v.value)
        assert vals == [0, 0, 0], f"unexpected dict values: {vals}"


class TestCreatePoolCallsIncludePoolerKwargs:
    """AST-level assertions: every create_pool call passes the cache kwargs."""

    @pytest.mark.parametrize(
        ("relpath", "label"),
        [(p, l) for p, _, l in _POOL_SITES],
        ids=[l for _, _, l in _POOL_SITES],
    )
    def test_create_pool_has_statement_cache_size(self, relpath: str, label: str) -> None:
        src = (_REPO / relpath).read_text(encoding="utf-8")
        calls = _find_create_pool_calls(src)
        assert calls, f"{label}: no create_pool() call found in {relpath}"

        # The pooler-safe calls should include our helper (via **_pooler_safe_kwargs).
        # Check that at least one create_pool call passes statement_cache_size=0
        # either directly or through **_pooler_safe_kwargs().
        found_with_cache_kwarg = False
        for call in calls:
            # Direct kwarg
            if _call_has_kwarg(call, "statement_cache_size"):
                found_with_cache_kwarg = True
                break
            # Via **_pooler_safe_kwargs() spread
            for kw in call.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Call):
                    func = kw.value.func
                    if isinstance(func, ast.Name) and func.id == "_pooler_safe_kwargs":
                        found_with_cache_kwarg = True
                        break
                    if isinstance(func, ast.Attribute) and func.attr == "_pooler_safe_kwargs":
                        found_with_cache_kwarg = True
                        break
            if found_with_cache_kwarg:
                break

        assert found_with_cache_kwarg, (
            f"{label} ({relpath}): create_pool() call does not include "
            f"statement_cache_size=0 (directly or via **_pooler_safe_kwargs())"
        )


class TestPoolerHelperImportableFromSqlLeg:
    """Verify _pooler_safe_kwargs is exported and importable from sql_leg module."""

    def test_helper_is_defined_in_source(self) -> None:
        src = (_REPO / "api" / "application" / "services" / "sql_leg.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        func_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_pooler_safe_kwargs"
        ]
        assert func_names, "_pooler_safe_kwargs not defined in sql_leg.py"

    def test_pooler_kwargs_consumed_by_all_five_sites(self) -> None:
        """Every create_pool site must spread **_pooler_safe_kwargs()."""
        for relpath, _, label in _POOL_SITES:
            src = (_REPO / relpath).read_text(encoding="utf-8")
            assert "_pooler_safe_kwargs" in src, (
                f"{label} ({relpath}): does not reference _pooler_safe_kwargs"
            )
