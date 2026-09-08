"""Read-only pgvector parity verifier — ISSUE-G4-01 / FR-37.

Proves the immutable safety invariant that every configured live vector
population matches its expected row count and every non-null embedding reports
vector_dims = 1024. The current production baseline for the Soleil corpus is
162/162/162/162 at dimension 1024.

This tool is strictly read-only: it opens a READ ONLY transaction, never
repairs, truncates, re-embeds, or mutates data. Output is diagnostic only; any
mismatch exits non-zero so migration orchestration can abort BEFORE mutation.

Secrets come from the existing POSTGRES_* environment variables only (via the
project settings, which read the environment/.env; nothing is hardcoded and
nothing is printed).

Usage:
    python scripts/verify_vector_parity.py \
        --expect-counts 162,162,162,162 --expect-dim 1024

    # or a single expected count for all populations:
    python scripts/verify_vector_parity.py --expect-count 162 --expect-dim 1024

Exit codes: 0 all populations pass; 1 at least one mismatch; 2 connection or
argument error (treat as UNKNOWN for operator investigation, never repair).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg2  # noqa: E402

from api.infrastructure.config.config import Settings, get_settings  # noqa: E402

# Vector populations that must stay 162/162/162/162 at dimension 1024, scoped
# to the configured project (default: the live Soleil corpus). The LightRAG
# entity/relation vector tables are intentionally excluded: they are per-graph
# populations with no 162 contract. %s binds the project filter pattern.
VDB_TABLE = "public.lightrag_vdb_chunks_text_embedding_v4_1024d"
VECTOR_POPULATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "registry_document_chunks",
        "public.document_chunks",
        "SELECT count(*) FROM public.document_chunks WHERE doc_id ILIKE %s",
    ),
    (
        "lightrag_doc_status",
        "public.lightrag_doc_status",
        "SELECT count(*) FROM public.lightrag_doc_status WHERE id ILIKE %s",
    ),
    (
        "lightrag_doc_chunks",
        "public.lightrag_doc_chunks",
        "SELECT count(*) FROM public.lightrag_doc_chunks WHERE id ILIKE %s",
    ),
    (
        "lightrag_vdb_chunks",
        VDB_TABLE,
        f"SELECT count(*) FROM {VDB_TABLE} WHERE id ILIKE %s",
    ),
)


@dataclass(frozen=True)
class PopulationResult:
    """One verifier outcome for a single vector population."""

    name: str
    table: str
    expected_count: int
    actual_count: int | None
    expected_dim: int
    dims: tuple[int, ...]
    count_ok: bool
    dims_ok: bool

    @property
    def passed(self) -> bool:
        return self.count_ok and self.dims_ok

    def format(self) -> str:
        if self.actual_count is None:
            return (
                f"[FAIL] {self.name}: table {self.table} missing "
                f"(expected {self.expected_count} rows, dims [{self.expected_dim}])"
            )
        dim_text = "no vectors" if not self.dims else ",".join(str(d) for d in self.dims)
        marker = "PASS" if self.passed else "FAIL"
        return (
            f"[{marker}] {self.name}: count {self.actual_count} "
            f"(expected {self.expected_count}) dims [{dim_text}] "
            f"(expected [{self.expected_dim}])"
        )


def _parse_counts(spec: str) -> list[int]:
    """Parse comma-separated counts; a single value is broadcast to all."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise ValueError("empty --expect-counts")
    try:
        values = [int(p) for p in parts]
    except ValueError as exc:  # non-integer input
        raise ValueError(f"invalid count in {spec!r}") from exc
    if len(values) == 1:
        return values * len(VECTOR_POPULATIONS)
    if len(values) != len(VECTOR_POPULATIONS):
        raise ValueError(
            f"--expect-counts needs 1 or {len(VECTOR_POPULATIONS)} values, got {len(values)}"
        )
    return values


def resolve_expected_counts(
    cli_single: int | None, cli_list: str | None, env_value: str | None
) -> list[int]:
    """Precedence: explicit --expect-count > --expect-counts > env override > baseline 162."""
    if cli_single is not None:
        return [cli_single] * len(VECTOR_POPULATIONS)
    spec = cli_list or env_value
    if spec:
        return _parse_counts(spec)
    return [162] * len(VECTOR_POPULATIONS)


def _connect(settings: Settings):
    return psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_database,
        connect_timeout=5,
    )


def verify_populations(
    conn,
    expected_counts: list[int],
    expected_dim: int,
    project_filter: str = "%soleil%",
) -> list[PopulationResult]:
    """Run every read-only count + vector_dims query on a READ ONLY transaction."""
    # Close any caller-open transaction first (set_session is illegal inside
    # one), then pin the session read-only so EVERY transaction — including
    # ones restarted after a diagnostic error — rejects writes.
    conn.rollback()
    conn.set_session(readonly=True)
    results: list[PopulationResult] = []
    with conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        for (name, table, count_sql), expected_count in zip(
            VECTOR_POPULATIONS, expected_counts, strict=True
        ):
            try:
                cur.execute(count_sql, (project_filter,))
                actual = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001 — missing/forbidden table is diagnostic, not fatal
                conn.rollback()
                results.append(
                    PopulationResult(
                        name=name,
                        table=table,
                        expected_count=expected_count,
                        actual_count=None,
                        expected_dim=expected_dim,
                        dims=(),
                        count_ok=False,
                        dims_ok=False,
                    )
                )
                continue
            dims = _query_vector_dims(cur, table)
            count_ok = actual == expected_count
            dims_ok = all(d == expected_dim for d in dims)
            results.append(
                PopulationResult(
                    name=name,
                    table=table,
                    expected_count=expected_count,
                    actual_count=actual,
                    expected_dim=expected_dim,
                    dims=dims,
                    count_ok=count_ok,
                    dims_ok=dims_ok,
                )
            )
    return results


def _query_vector_dims(cur, table: str) -> tuple[int, ...]:
    """Distinct non-null vector_dims() over every vector column in the table.

    Runs on the caller's READ ONLY cursor. Identifiers come from
    pg_attribute/pg_class only, so quoting them is safe.
    """
    dims: set[int] = set()
    bare_table = table.split(".")[-1]
    cur.execute(
        """
        SELECT a.attname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND t.typname = 'vector'
          AND a.attnum > 0
          AND NOT a.attisdropped
        """,
        (bare_table,),
    )
    cols = [r[0] for r in cur.fetchall()]
    for col in cols:
        cur.execute(
            f"SELECT DISTINCT vector_dims({psycopg2.extensions.quote_ident(col, cur.connection)}) "
            f"FROM public.{psycopg2.extensions.quote_ident(bare_table, cur.connection)} "
            f"WHERE {psycopg2.extensions.quote_ident(col, cur.connection)} IS NOT NULL"
        )
        dims.update(int(r[0]) for r in cur.fetchall())
    return tuple(sorted(dims))


def _print_report(results: list[PopulationResult]) -> int:
    all_pass = True
    for res in results:
        print(res.format())
        all_pass = all_pass and res.passed
    return 0 if all_pass else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--expect-counts",
        default=None,
        help="comma-separated expected row count per population (default: env override then 162x4)",
    )
    parser.add_argument(
        "--expect-count",
        type=int,
        default=None,
        help="broadcast single expected row count for all populations",
    )
    parser.add_argument(
        "--expect-dim",
        type=int,
        default=1024,
        help="expected embedding dimension (LOCKED 1024)",
    )
    parser.add_argument(
        "--project-filter",
        default="%soleil%",
        help="ILIKE pattern scoping the live populations (default the Soleil corpus)",
    )
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] settings unavailable: {exc}", file=sys.stderr)
        return 2

    try:
        expected_counts = resolve_expected_counts(
            args.expect_count,
            args.expect_counts,
            os.getenv("VECTOR_PARITY_EXPECTED_COUNTS"),
        )
    except ValueError as exc:
        print(f"[ERROR] bad --expect-counts: {exc}", file=sys.stderr)
        return 2

    try:
        conn = _connect(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] cannot connect to Postgres: {exc}", file=sys.stderr)
        return 2

    try:
        results = verify_populations(
            conn, expected_counts, args.expect_dim, args.project_filter
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] verifier failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    return _print_report(results)


if __name__ == "__main__":
    raise SystemExit(main())
