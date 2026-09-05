"""FR-25 (revised) — training scope: shared project corpus, lookup profile.

Matrix (business decision 2026-09: training and client chat share ONE project
corpus; documents.kind is only ever legal|price|project and no 'training' kind
will ever exist):
  1. Typed contract: the '_training' marker stays reserved + is_training_scope,
     and training_retrieval_scope maps every training leg to the REAL context
     project (never the marker) — project isolation stays absolute.
  2. rag_leg._post_filter: one shared project-filter matrix (kind layers gone);
     training turns scope exactly like customer turns.
  3. build_messages: training prompt profile differs from customer, and the
     sales kit / persona directive / identity placeholders never appear.
  4. No training-specific grounding gate exists any more: training degrades to
     the same ungrounded handling as customer mode.
  5. /query integration (SSE + JSON): the pipeline receives the marker scope
     plus the real context project, CTA suppression stays intact, and a grounded
     lookup answer streams normally.

All offline: every DB/LLM seam is a recording fake — no external data touched,
no credentials or PII in any assertion.
"""

from __future__ import annotations

import inspect
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.application.pipelines.workflow import training_retrieval_scope
from api.application.services import rag_leg
from api.application.services.generate import (
    _TRAINING_PROMPT,
    build_messages,
    select_answer_tier,
)
from api.application.services.merge import Merged
from api.application.services.project_scope import (
    TRAINING_PROJECT_KEY,
    ProjectScopeError,
    is_training_scope,
    validate_project_key,
)
from api.interfaces.api.main import QueryRequest, create_app
from tests._auth_seams import sales_bearer_headers
from tests._training_seams import install_training_seams, training_payload

# --- 1. typed contract ---------------------------------------------------------


def test_training_marker_is_reserved_and_never_a_client_scope() -> None:
    assert is_training_scope(TRAINING_PROJECT_KEY)
    assert not is_training_scope("camellia")
    assert not is_training_scope(None)
    with pytest.raises(ProjectScopeError):
        validate_project_key(TRAINING_PROJECT_KEY)  # client can never name it
    # The impossible kind='training' corpus concept is fully removed.
    import api.application.services.project_scope as ps

    assert not hasattr(ps, "TrainingCorpusEmptyError")
    assert not hasattr(ps, "TRAINING_DOC_KIND")
    assert not hasattr(ps, "TRAINING_CORPUS_EMPTY_CODE")


def test_training_retrieval_scope_maps_marker_to_real_project() -> None:
    """Single source: every training leg rides the REAL context project."""
    assert training_retrieval_scope("_training", "camellia") == "camellia"
    # Unscoped training must NOT fall back to the marker (which matches no doc)
    # nor to None (which would disable project isolation).
    assert training_retrieval_scope("_training", None) is None
    # Customer mode passes through byte-identically.
    assert training_retrieval_scope("camellia", None) == "camellia"
    assert training_retrieval_scope(None, None) is None


def test_training_mode_still_validates_request_shape() -> None:
    assert QueryRequest(
        query="lookup", answer_mode="training", context={"project_key": "camellia"}
    ).answer_mode == "training"
    # G3-r6 §10.2: training is meaningless without its context project and
    # normal mode must never smuggle one in — both directions reject at 422.
    with pytest.raises(ValidationError):
        QueryRequest(query="lookup", answer_mode="training")
    with pytest.raises(ValidationError):
        QueryRequest(query="lookup", context={"project_key": "camellia"})


# --- 2. _post_filter shared project matrix (kind layers removed) ---------------


class _FakeConn:
    def __init__(self, rows: list[dict], capture: dict) -> None:
        self._rows = rows
        self._capture = capture

    async def fetch(self, sql: str, *args):
        self._capture["sql"] = sql
        self._capture["args"] = args
        return self._rows


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)

    def __await__(self):
        async def _self():
            return self

        return _self().__await__()


def _row(chunk_id: str, *, kind: str, project_key: str | None, status: str = "published",
         effective_to: date | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": f"doc-{chunk_id}",
        "status": status,
        "effective_from": None,
        "effective_to": effective_to,
        "project_key": project_key,
        "kind": kind,
    }


def _patch_pool(monkeypatch, rows: list[dict], capture: dict) -> None:
    conn = _FakeConn(rows, capture)
    monkeypatch.setattr(rag_leg, "get_ro_pool", lambda: _FakePool(conn))


@pytest.mark.asyncio
async def test_training_scope_keeps_all_project_doc_kinds_regardless_of_kind(
    monkeypatch,
) -> None:
    """Shared corpus: a training turn scoped to the real project keeps EVERY
    published kind of that project and drops other projects' docs."""
    chunks = [{"id": f"c{i}"} for i in range(4)]
    rows = [
        _row("c0", kind="price", project_key="camellia"),      # keep
        _row("c1", kind="legal", project_key="camellia"),      # keep
        _row("c2", kind="project", project_key="soleil"),      # drop: other project
        _row("c3", kind="price", project_key="camellia", status="draft"),  # drop
    ]
    capture: dict = {}
    _patch_pool(monkeypatch, rows, capture)
    # The training scope reaching the filter is the REAL project key (mapped by
    # training_retrieval_scope in the legs), never '_training'.
    scope = training_retrieval_scope("_training", "camellia")
    kept = await rag_leg._post_filter(chunks, None, project_key=scope)
    assert [c["id"] for c in kept] == ["c0", "c1"]
    # SQL predicate is the project filter ONLY — the kind COLUMN is still
    # selected (source metadata) but no longer a filter predicate.
    assert "d.project_key = $2" in capture["sql"]
    assert "d.kind = $" not in capture["sql"]
    assert capture["args"][1] == "camellia"


@pytest.mark.asyncio
async def test_training_scope_still_enforces_effectivity(monkeypatch) -> None:
    chunks = [{"id": "c0"}]
    rows = [_row("c0", kind="price", project_key="camellia", effective_to=date(2026, 1, 1))]
    capture: dict = {}
    _patch_pool(monkeypatch, rows, capture)
    kept = await rag_leg._post_filter(
        chunks, date(2026, 8, 27), project_key=training_retrieval_scope("_training", "camellia")
    )
    assert kept == []


@pytest.mark.asyncio
async def test_customer_filter_path_unchanged_by_training_revision(monkeypatch) -> None:
    chunks = [{"id": "c0"}, {"id": "c1"}]
    rows = [
        _row("c0", kind="price", project_key="camellia"),
        _row("c1", kind="price", project_key="soleil"),
    ]
    capture: dict = {}
    _patch_pool(monkeypatch, rows, capture)
    kept = await rag_leg._post_filter(chunks, None, project_key="camellia")
    assert [c["id"] for c in kept] == ["c0"]
    assert "d.project_key = $2" in capture["sql"]
    assert "d.kind = $" not in capture["sql"]


def test_rag_leg_signature_has_no_kind_layers() -> None:
    """The doc_kind/training_project_key filter layers are gone from the API."""
    params = set(inspect.signature(rag_leg.run_rag_leg).parameters)
    assert "doc_kind" not in params
    assert "training_project_key" not in params


# --- 3. training prompt profile ------------------------------------------------


def _merged(project_key: str, **extra_meta) -> Merged:
    meta = {"rewritten": "Câu hỏi tra cứu dự án", "query": "Câu hỏi tra cứu dự án", "project_key": project_key}
    meta.update(extra_meta)
    return Merged(rag_blocks="RAG", evidence_blocks="EV", sources=[], facts=[], meta=meta)


_FORBIDDEN_TRAINING_MARKERS = (
    "SALES_CONTEXT",
    "CONVERSATION_DIRECTIVE",
    "lead_cta_hint",
    "mời khách để lại số",
    "cuộc gọi 5 phút",
    "{ten_thuong_mai}",
    "chuyên viên tư vấn cao cấp",
)


def test_training_messages_use_training_profile_and_drop_persona_and_cta() -> None:
    merged = _merged(
        TRAINING_PROJECT_KEY,
        conversation_directive="Recap + MỘT lời mời nhận cuộc gọi 5 phút (CTA).",
        lead_cta_hint="customer CTA",
    )
    messages = build_messages(merged, [])
    system = [m for m in messages if m["role"] == "system"]
    assert len(system) == 1  # no CONVERSATION_DIRECTIVE message in training
    assert system[0]["content"] == _TRAINING_PROMPT
    blob = json.dumps(messages, ensure_ascii=False)
    for marker in _FORBIDDEN_TRAINING_MARKERS:
        assert marker not in blob
    assert "sales_context_injected" not in merged.meta


def test_customer_messages_keep_profile_identity_and_directive() -> None:
    merged = _merged("camellia", conversation_directive="chào ấm 1 câu")
    messages = build_messages(merged, [])
    system = [m for m in messages if m["role"] == "system"]
    assert system[0]["content"] == messages[0]["content"]
    # The customer profile is the OTHER prompt, and the directive survives.
    assert system[0]["content"] != _TRAINING_PROMPT
    assert any("CONVERSATION_DIRECTIVE" in m["content"] for m in system if m["role"] == "system")
    # No unrendered placeholders may leak into the customer system prompt.
    assert "{ten_thuong_mai}" not in system[0]["content"]


def test_training_prompt_differs_from_customer_prompt() -> None:
    from api.application.services.generate import _SYSTEM_PROMPT

    assert _TRAINING_PROMPT != _SYSTEM_PROMPT
    assert "TRA CỨU THÔNG TIN DỰ ÁN" in _TRAINING_PROMPT.upper()


def test_training_turns_get_the_pro_answer_tier() -> None:
    """Data-dense staff lookups always earn the richer pro budget; the customer
    matrix (greet/lookup -> flash) stays byte-identical."""
    training = _merged(TRAINING_PROJECT_KEY, conv_state=None)
    assert select_answer_tier(training, high_stakes=False) == "pro"
    customer_flash = _merged("camellia", conv_state="greet")
    assert select_answer_tier(customer_flash, high_stakes=False) == "flash"


# --- 4. no training grounding gate ---------------------------------------------


def test_training_grounding_gate_is_removed() -> None:
    import api.application.pipelines.workflow as wf

    assert not hasattr(wf, "training_grounding_gate")


# --- 5. /query integration -------------------------------------------------------

_PAYLOAD = {
    "answer": "ok",
    "sources": [],
    "facts": [],
    "images": [],
    "videos": [],
    "places": [],
    "confidence": "HIGH",
    "requires_review": False,
    "routing": {"intent": "rag"},
    "trace_id": "t-1",
    "latency_ms": 1,
}


class _RecordingGate:
    def __init__(self) -> None:
        self.refunds = 0
        self.prepared: list[dict] = []

    async def prepare_turn(self, **kwargs):
        self.prepared.append(kwargs)
        return _AttributedTurnContext()

    async def refund_turn(self, context):
        self.refunds += 1


class _AttributedTurnContext:
    """Duck-typed managed context: main's refund path requires both attrs."""

    identity_key = "dev:device-training-test"
    project_key = TRAINING_PROJECT_KEY

    def quota_payload(self) -> dict:
        return {"used_turns": None}


@pytest.fixture()
def gate() -> _RecordingGate:
    return _RecordingGate()


@pytest.fixture()
def client(monkeypatch, gate: _RecordingGate):
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate", lambda: gate
    )

    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key", _fake_resolve
    )
    return TestClient(create_app())


def _use_pipeline(monkeypatch, run_impl) -> None:
    class FakePipeline:
        run = run_impl

    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", FakePipeline
    )


def test_json_training_routes_marker_plus_real_context(
    client, gate, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    """Shared corpus: the pipeline gets the marker scope AND the real context
    project; a normal grounded answer streams with ok=True (no empty-corpus
    failure mode exists any more)."""
    install_training_seams(monkeypatch)

    async def ok_run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        assert kwargs["project_key"] == TRAINING_PROJECT_KEY  # marker (prompt profile)
        assert kwargs["training_context_project_key"] == "camellia"  # data scope
        return {**_PAYLOAD, "answer": "lookup answer", "lead_cta_hint": "customer CTA"}

    _use_pipeline(monkeypatch, ok_run)
    resp = client.post(
        "/query",
        json=training_payload(),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Success contract: no structured error frame — the empty-corpus failure
    # mode no longer exists (a grounded lookup completes normally).
    assert "error" not in body
    assert body["status"] == "completed"
    assert body["answer"] == "lookup answer"
    assert body["lead_cta_hint"] is None  # CTA suppression contract intact
    assert body["context"] == {"project_key": "camellia"}
    assert gate.refunds == 0  # a successful turn is never refunded


def test_sse_training_streams_answer_without_error_frame(
    client, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    install_training_seams(monkeypatch)

    async def ok_run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        if on_event is not None:
            await on_event("token", {"text": "tra cứu xong"})
        return {**_PAYLOAD, "answer": "tra cứu xong"}

    _use_pipeline(monkeypatch, ok_run)
    with client.stream(
        "POST",
        "/query",
        json=training_payload(),
        headers={
            "accept": "text/event-stream",
            **sales_bearer_headers(local_rsa_jwk),
        },
    ) as stream:
        raw = "".join(stream.iter_text())
    assert "event: error" not in raw
    assert "TRAINING_CORPUS_EMPTY" not in raw
    assert "event: done" in raw


def test_sse_training_grounding_and_cta_suppression(
    client, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    """Grounded training turn: shared project-corpus sources ride through, zero CTA."""

    async def ok_run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        if on_event is not None:
            await on_event("routing", {"intent": "rag", "lead_cta_hint": "customer CTA"})
        return {
            **_PAYLOAD,
            "answer": "lookup answer",
            "sources": [{"doc_id": "price-a", "kind": "price", "title": "T"}],
            "lead_cta_hint": "customer CTA",
            "project_redirect": {"project_key": "camellia"},
            "conversation_directive": "sales directive",
        }

    _use_pipeline(monkeypatch, ok_run)
    resp = client.post(
        "/query",
        json=training_payload(),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    body = resp.json()
    assert body["sources"][0]["kind"] == "price"
    assert body["lead_cta_hint"] is None
    assert body["project_redirect"] is None
