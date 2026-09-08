"""Regression tests — project-scoped retrieval isolation (Soleil/Camellia).

Root cause (verified against the live registry): ingest.load_document never
wrote documents.project_key, so the 2026-08-21 campaign-only backfill left 27/31
documents on the reserved '_legacy' key. The retrieval post-filter
(rag_leg._post_filter) and the SQL-leg predicates require `project_key = <p>`,
so a Soleil "chính sách bán hàng" query dropped its OWN policy/legal chunks
(ungrounded, sources=[]) while the shared LightRAG store still surfaced Camellia
chunks carrying MBLand/Thành Lâm content.

These tests pin the fix at each layer without a live DB:

- ingest: load_document persists project_key on the documents row and the
  runners tag their corpus explicitly.
- store: the doc_id -> project_key derivation maps Soleil/Camellia docs.
- retrieval: _post_filter keeps only the requested project's chunks (a Soleil
  query never keeps a Camellia chunk, and vice versa).
- query/fallback: FACT-placeholder hydration (merge._resolve_fact_value) is
  scoped by project_key so a chunk never pulls a fact from another project.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import date
from unittest.mock import patch

import pytest

from ingest.parser import ParsedDoc, ParsedSection


async def _noop(*args, **kwargs):
    return None


@contextmanager
def _no_lightrag():
    """No-op the post-COMMIT LightRAG step so unit tests never touch a real
    LightRAG/PG store (no live DB). load_document imports the helpers inside the
    function body, so the source module — not the load module — is patched."""
    with ExitStack() as stack:
        stack.enter_context(patch("ingest.lightrag_init.get_lightrag", return_value=object()))
        stack.enter_context(patch("ingest.lightrag_init.ainsert_document", new=_noop))
        stack.enter_context(patch("ingest.lightrag_init.adelete_by_doc_id", new=_noop))
        yield


# --- ingest: load_document writes documents.project_key -----------------------


def _parsed(doc_id: str, project_key: str | None = None) -> ParsedDoc:
    return ParsedDoc(
        doc_id=doc_id,
        title=f"Doc {doc_id}",
        kind="legal",
        source_file=f"data/{doc_id}.pdf",
        sections=[ParsedSection(text="Nội dung"), ParsedSection(text="Nội dung 2")],
        content_hash="abc123",
        effective_from=date(2026, 1, 1),
        project_key=project_key,
    )


class _FakeConn:
    """asyncpg connection double recording the documents-upsert call.

    fetchrow is called for the documents upsert first (returns version), then
    for _upsert_subject/_insert_fact (returns ids); fetch/execute return empty.
    """

    def __init__(self) -> None:
        self.doc_upsert_sql: str | None = None
        self.doc_upsert_args: tuple | None = None

    def transaction(self):
        return _FakeTx(self)

    async def fetchrow(self, sql: str, *args):
        if self.doc_upsert_sql is None:
            self.doc_upsert_sql = sql
            self.doc_upsert_args = args
            return {"version": 1}
        return {"id": 1}

    async def fetch(self, sql: str, *args):
        return []

    async def execute(self, sql: str, *args):
        return None

    async def close(self):
        return None


class _FakeTx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_load_document_persists_project_key_on_documents_row() -> None:
    import ingest.load as load_mod

    conn = _FakeConn()
    with patch.object(load_mod.asyncpg, "connect", return_value=conn), _no_lightrag():
        # Explicit runner-provided project key wins over ParsedDoc/doc_id.
        result = await load_mod.load_document(
            _parsed("price-soleil-2026q3-policy"), project_key="soleil"
        )
    assert result.doc_id == "price-soleil-2026q3-policy"
    assert conn.doc_upsert_sql is not None
    assert "project_key" in conn.doc_upsert_sql
    # $1 doc_id ... $8 project_key ... $9 metadata
    assert conn.doc_upsert_args[7] == "soleil"


@pytest.mark.asyncio
async def test_load_document_falls_back_to_doc_id_derived_project() -> None:
    import ingest.load as load_mod

    conn = _FakeConn()
    with patch.object(load_mod.asyncpg, "connect", return_value=conn), _no_lightrag():
        await load_mod.load_document(_parsed("legal-soleil-chu-truong-2018"))
    assert conn.doc_upsert_args[7] == "soleil"


@pytest.mark.asyncio
async def test_load_document_keeps_null_when_project_unresolvable() -> None:
    """A doc whose project cannot be inferred stays NULL (never mis-scoped)."""
    import ingest.load as load_mod

    conn = _FakeConn()
    with patch.object(load_mod.asyncpg, "connect", return_value=conn), _no_lightrag():
        await load_mod.load_document(_parsed("nd-101-2024"))
    assert conn.doc_upsert_args[7] is None


# --- store: doc_id -> project_key derivation ----------------------------------


def test_derive_project_key_maps_soleil_and_camellia_docs() -> None:
    from ingest.load import _derive_project_key

    assert _derive_project_key("price-soleil-2026q3-policy") == "soleil"
    assert _derive_project_key("price-camellia-2026q3-payment") == "camellia"
    assert _derive_project_key("legal-soleil-chu-truong-2018") == "soleil"
    assert _derive_project_key("project-soleil-qna") == "soleil"
    assert _derive_project_key("nd-101-2024") is None


def test_soleil_runner_tags_corpus_explicitly() -> None:
    from ingest.run_soleil_ingest import PROJECT_KEY

    assert PROJECT_KEY == "soleil"


def test_camellia_runner_tags_corpus_explicitly() -> None:
    from ingest.run_camellia_ingest import PROJECT_KEY

    assert PROJECT_KEY == "camellia"


# --- retrieval: _post_filter keeps only the requested project's chunks --------


@pytest.mark.asyncio
async def test_post_filter_soleil_query_keeps_only_soleil_policy_chunks() -> None:
    """Regression for the reported bug: a Soleil sales-policy query must keep the
    Soleil policy chunks and drop the Camellia policy chunks, even though the
    shared LightRAG store returns both."""
    from api.application.services import rag_leg

    chunks = [
        {
            "id": "price-soleil-2026q3-policy:3:0",
            "score": 0.9,
            "content": "CSBH Soleil",
            "file_path": "price-soleil-2026q3-policy:3:0",
        },
        {
            "id": "price-camellia-2026q3-policy:3:0",
            "score": 0.85,
            "content": "MBLand CSBH",
            "file_path": "price-camellia-2026q3-policy:3:0",
        },
    ]
    recs = [
        {
            "chunk_id": "price-soleil-2026q3-policy:3:0",
            "doc_id": "price-soleil-2026q3-policy",
            "status": "published",
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "project_key": "soleil",
        },
        {
            "chunk_id": "price-camellia-2026q3-policy:3:0",
            "doc_id": "price-camellia-2026q3-policy",
            "status": "published",
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "project_key": "camellia",
        },
    ]

    class _FakePool:
        def acquire(self):
            return _FakeConn(recs)

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch(self, sql, *args):
            assert "d.project_key" in sql
            return self._rows

    with patch.object(rag_leg, "get_ro_pool", return_value=_FakePool()):
        kept = await rag_leg._post_filter(chunks, None, project_key="soleil")

    assert [c["id"] for c in kept] == ["price-soleil-2026q3-policy:3:0"]


@pytest.mark.asyncio
async def test_post_filter_legacy_doc_never_grounds_a_project_query() -> None:
    """A doc still on the reserved '_legacy' key must NOT leak into a project
    answer — it is invisible until a human tags it."""
    from api.application.services import rag_leg

    chunks = [
        {
            "id": "legal-soleil-chu-truong-2018:1:0",
            "score": 0.9,
            "content": "x",
            "file_path": "legal-soleil-chu-truong-2018:1:0",
        }
    ]
    recs = [
        {
            "chunk_id": "legal-soleil-chu-truong-2018:1:0",
            "doc_id": "legal-soleil-chu-truong-2018",
            "status": "published",
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "project_key": "_legacy",
        },
    ]

    class _FakePool:
        def acquire(self):
            return _FakeConn(recs)

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch(self, sql, *args):
            return self._rows

    with patch.object(rag_leg, "get_ro_pool", return_value=_FakePool()):
        kept = await rag_leg._post_filter(chunks, None, project_key="soleil")
    assert kept == []


# --- query/fallback: FACT-placeholder hydration is project-scoped -------------


@pytest.mark.asyncio
async def test_resolve_fact_value_scopes_project_predicate() -> None:
    """Placeholder hydration carries the project predicate in SQL, so a Soleil
    chunk can never resolve a value from a Camellia subject with the same code."""
    from api.application.services import merge as merge_mod

    captured: dict = {}

    class _FakeConn:
        async def fetchrow(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return {
                "value_num": 1_900_000_000,
                "value_text": None,
                "unit": "tỷ đồng",
                "quality": "exact",
                "range_min": None,
                "range_max": None,
            }

    value = await merge_mod._resolve_fact_value(
        _FakeConn(),
        "price_vnd",
        "unit:soleil/1pn",
        "chuan",
        date(2026, 8, 1),
        project_key="soleil",
    )
    assert value is not None
    assert "fs.project_key = $4" in captured["sql"]
    assert captured["args"][3] == "soleil"
