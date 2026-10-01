"""Project-scoped retrieval and training prompt/transport contracts.

All offline: every DB/LLM seam is a recording fake — no external data touched,
no credentials or PII in any assertion.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.application.ports.project_registry import ProjectRegistryRecord
from api.application.services import rag_leg
from api.application.services.project_config import bound_request_project, fetch_project_identity
from api.application.services.generate import (
    _TRAINING_PROMPT,
    build_messages,
    select_answer_tier,
)
from api.application.services.merge import Merged
from api.application.services.project_context import ProjectContext
from api.interfaces.api.main import QueryRequest, create_app
from tests._auth_seams import sales_bearer_headers
from tests._training_seams import install_training_seams, training_payload
 



def test_fetch_project_identity_uses_only_matching_bound_snapshot() -> None:
    project_key = "project_alpha"
    matching_record = ProjectRegistryRecord(
        project_key=project_key,
        ten_thuong_mai="Alpha Homes",
        ten_phap_ly="Alpha Realty Ltd.",
        vi_tri="Da Nang",
        hotline="0900000000",
        location="Da Nang",
        geo_center_lat=None,
        geo_center_lng=None,
        is_hot=False,
        status="active",
    )
    camellia_record = ProjectRegistryRecord(
        project_key="camellia",
        ten_thuong_mai="Camellia Static Name",
        ten_phap_ly=None,
        vi_tri="Camellia Static Location",
        hotline=None,
        location=None,
        geo_center_lat=None,
        geo_center_lng=None,
        is_hot=False,
        status="active",
    )

    with bound_request_project(matching_record):
        assert fetch_project_identity(project_key) == {
            "ten_thuong_mai": "Alpha Homes",
            "ten_phap_ly": "Alpha Realty Ltd.",
            "vi_tri": "Da Nang",
            "hotline": "0900000000",
        }

    for record in (camellia_record, None):
        with bound_request_project(record):
            assert fetch_project_identity(project_key) == {}


def test_training_context_uses_real_project_key() -> None:
    context = ProjectContext("camellia", "training", (("camellia", "Camellia"),))
    assert context.project_key == "camellia"
    assert context.answer_mode == "training"


def test_training_mode_still_validates_request_shape() -> None:
    assert QueryRequest(
        query="lookup", answer_mode="training", context={"project_key": "camellia"}
    ).answer_mode == "training"
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
    kept = await rag_leg._post_filter(chunks, None, project_key="camellia")
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
        chunks, date(2026, 8, 27), project_key="camellia"
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
        "camellia",
        answer_mode="training",
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
    training = _merged("camellia", answer_mode="training", conv_state=None)
    assert select_answer_tier(training, high_stakes=False) == "pro"
    customer_flash = _merged("camellia", conv_state="greet")
    assert select_answer_tier(customer_flash, high_stakes=False) == "flash"


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
    project_key = "camellia"

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

    from api.application.ports.project_registry import ProjectRegistryRecord

    async def fetch_records():
        return [ProjectRegistryRecord(
            project_key="camellia", ten_thuong_mai="Camellia", ten_phap_ly=None,
            vi_tri=None, hotline=None, location=None, geo_center_lat=None,
            geo_center_lng=None, is_hot=False, status="active",
        )]
    monkeypatch.setattr("api.application.services.project_scope.fetch_active_project_records", fetch_records)
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


def test_json_training_routes_project_context(
    client, gate, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    """Training keeps the real project scope and suppresses customer CTAs."""
    install_training_seams(monkeypatch)

    async def ok_run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        context = kwargs["project_context"]
        assert context.project_key == "camellia"
        assert context.answer_mode == "training"
        return {**_PAYLOAD, "answer": "lookup answer", "lead_cta_hint": "customer CTA"}

    _use_pipeline(monkeypatch, ok_run)
    resp = client.post(
        "/query",
        json=training_payload(),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    assert resp.status_code == 200
    body = resp.json()
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
        context = kwargs["project_context"]
        assert context.project_key == "camellia"
        assert context.answer_mode == "training"
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
    assert "event: done" in raw


def test_sse_training_grounding_and_cta_suppression(
    client, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    """Grounded training turn: shared project-corpus sources ride through, zero CTA."""
    install_training_seams(monkeypatch)

    async def ok_run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        context = kwargs["project_context"]
        assert context.project_key == "camellia"
        assert context.answer_mode == "training"
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


def test_training_soleil_context_is_owner_bound_despite_camellia_phrase(
    client, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    """Level-B scenario 12: a Camellia phrase in the question never moves a
    Soleil training turn. The real inner workflow runs (only the LLM/DB seams
    are offline), so the project key asserted at the leg is the scope that
    actually reached retrieval — not a value restated from the request."""
    from functools import wraps

    from api.application.pipelines import conv_workflow, workflow
    from api.application.services import project_scope

    repo = install_training_seams(monkeypatch, statuses={"soleil": "active"})

    async def load_registry_record(project_key):
        if project_key not in {"camellia", "soleil"}:
            return None
        return ProjectRegistryRecord(
            project_key=project_key,
            ten_thuong_mai=project_key.title(),
            ten_phap_ly=None, vi_tri=None, hotline=None, location=None,
            geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active",
        )

    # The shared seam's minimal record satisfies the preflight, but the real
    # workflow also reads identity off the request-bound snapshot, so this
    # test supplies the full registry record shape.
    monkeypatch.setattr(
        "api.application.services.project_config.load_project_registry_record",
        load_registry_record,
    )

    async def fetch_active_project_records():
        return [
            ProjectRegistryRecord(
                project_key=key, ten_thuong_mai=name, ten_phap_ly=None,
                vi_tri=None, hotline=None, location=None, geo_center_lat=None,
                geo_center_lng=None, is_hot=False, status="active",
            )
            for key, name in (("camellia", "Camellia"), ("soleil", "Soleil"))
        ]

    resolver_calls: list = []

    async def observe_resolve(requested, *, active_projects=None):
        resolver_calls.append(requested)
        return "camellia"  # the wrong answer on purpose: training must not ask

    monkeypatch.setattr(project_scope, "fetch_active_project_records", fetch_active_project_records)
    monkeypatch.setattr(project_scope, "resolve_project_key", observe_resolve)

    contexts: list = []
    rag_project_keys: list = []
    image_project_keys: list = []

    original_facade_run = conv_workflow.RagQueryPipelineConv.run

    @wraps(original_facade_run)
    async def observe_facade_run(self, *args, **kwargs):
        contexts.append(kwargs["project_context"])
        return await original_facade_run(self, *args, **kwargs)

    monkeypatch.setattr(conv_workflow.RagQueryPipelineConv, "run", observe_facade_run)

    async def rewrite_query(query, history, as_of):
        return workflow.RoutedResult(
            rewritten=query,
            routing={
                "needs_rag": True,
                "needs_sql": False,
                "needs_geo": False,
                "structured_path": "none",
                "high_stakes": False,
            },
            sql_spec=None,
            hl_keywords=[],
            ll_keywords=[],
            high_stakes=False,
            as_of=as_of,
        )

    async def offline_rag_leg(rewritten, hl, ll, as_of, project_key=None):
        rag_project_keys.append(project_key)
        return workflow.RagLegResult([], degraded=False)

    class OfflineReranker:
        async def rerank(self, query, chunks):
            return chunks

    async def no_images(*args, project_key=None, **kwargs):
        image_project_keys.append(project_key)
        return []

    async def offline_answer(*args, **kwargs):
        yield "Soleil tra cứu xong"

    async def no_audit(*args, **kwargs):
        return None

    async def no_pg2(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(workflow, "rewrite_query", rewrite_query)
    monkeypatch.setattr(workflow, "run_rag_leg", offline_rag_leg)
    monkeypatch.setattr(workflow, "get_reranker", lambda: OfflineReranker())
    monkeypatch.setattr(workflow, "search_images", no_images)
    monkeypatch.setattr(workflow, "stream_answer", offline_answer)
    monkeypatch.setattr(workflow, "write_audit", no_audit)
    monkeypatch.setattr("api.domain.services.guard_input._pg2_check", no_pg2)

    resp = client.post(
        "/query",
        json=training_payload(
            query="camellia có gì bán không?", context={"project_key": "soleil"}
        ),
        headers=sales_bearer_headers(local_rsa_jwk),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["context"] == {"project_key": "soleil"}

    # Owner-bound context: the context project, in training mode, exactly once.
    assert len(contexts) == 1
    assert contexts[0].project_key == "soleil"
    assert contexts[0].answer_mode == "training"
    assert resolver_calls == []  # the training preflight supplied the scope

    # Retrieval authority: the leg and the imagery read Soleil only, so the
    # Camellia phrase in the text bought no Camellia scope anywhere.
    assert rag_project_keys == ["soleil"]
    assert image_project_keys == ["soleil"]
    assert "camellia" not in rag_project_keys + image_project_keys

    # The foreign-project redirect is a customer-mode behaviour; a training
    # turn answers in its own context instead of bouncing to Camellia.
    assert body["project_redirect"] is None
    assert repo.bound == {"s-training-1": ("uid-sales-mapped", "soleil")}
    assert repo.customer_append_calls == 0
