"""Hermetic Zalo E2E: real webhook route -> real processor -> REAL pipeline -> fake sender.

The gap this closes: no existing harness joins ingress, the transport processor
and the real retrieval chain in one test. ``tests/test_zalo_bot_ingress.py``
stops at the scheduler, ``tests/test_zalo_bot_processor.py`` injects a MOCK
pipeline, and ``tests/levelb_registry_harness.py`` runs the real pipeline
outside the Zalo path.

Here the REAL ``ZaloBotProcessor`` drives the REAL ``GlobalAssistantService``
whose ``pipeline_factory`` IS ``RagQueryPipelineConv``. Seam discipline is the
Level B one (reused verbatim): LightRAG, the LLM stream, the input guard, the
rewriter, the SQL leg and the price probe are faked; ``run_rag_leg``,
``_post_filter``, ``merge_context``, ``build_messages``, ``guard_output`` and
the whole ``RagRgreConvWorkflow`` shell are production code.

Level B reaches those same production reads through a disposable PostgreSQL
registry; that database is not reachable here (and would not be hermetic), so
the SAME two JOINs are answered by an in-memory registry that filters exactly
the way the SQL does — the project/status/validity predicates live in the
production ``_post_filter`` and ``_eligible_chunk``, not in this file.

Nothing leaves the process: no PG, no Secret Manager, no provider, no network.
``zalo_bot_open_demo_access=False``, so access is decided by the store's
binding lookup; the identity secret is a test-only constant.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.application.pipelines.conv_workflow import RagQueryPipelineConv
from api.application.services import zalo_bot_dispatch
from api.application.services.global_assistant_service import GlobalAssistantService
from api.application.services.project_config import ProjectRegistryRecord
from api.application.services.zalo_bot_identity import opaque_identity
from api.application.services.zalo_bot_processor import ZaloBotProcessor, ZaloEvent
from api.interfaces.api import zalo_bot_routes
from tests.levelb_registry_harness import LevelBHarness
from tests.test_zalo_bot_processor import (
    CHAT,
    SECRET,
    DeduplicatingAdmission,
    MemoryHistory,
    MemoryStore,
)

WEBHOOK_PATH = "/api/integrations/zalo-bot/webhook"
WEBHOOK_SECRET = "test-only-webhook-secret-1234"
QUERY = "Camellia có tiện ích gì?"
ANSWER = "Camellia có vườn cây xanh theo Hồ sơ tiện ích Camellia."
CAMELLIA_CHUNK = "camellia-amenities"
SOLEIL_CHUNK = "soleil-pool-deck"
CAMELLIA_CONTENT = "Camellia có vườn cây xanh và lối dạo bộ."
# Only this chunk belongs to Soleil; it must never reach a Camellia turn.
SOLEIL_CONTENT = "Soleil có bể bơi trên cao 40 tầng."
START = (date.today() - timedelta(days=30)).isoformat()


def _registry_row(chunk_id: str, content: str, *, doc_id: str, project_key: str,
                  title: str, section: str = "Tiện ích") -> dict:
    """One registry row: the shared shape of both production JOINs."""
    return {
        "chunk_id": chunk_id,
        "registry_content": content,
        "section": section,
        "doc_id": doc_id,
        "title": title,
        "kind": "project",
        "project_key": project_key,
        "status": "published",
        "effective_from": date(2020, 1, 1),
        "effective_to": None,
    }


class _Connection:
    """Serves the two registry JOINs with the SAME predicates as their SQL."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def fetch(self, sql: str, *args):
        ids = list(args[0] or [])
        project_key = args[1] if len(args) > 1 else None
        rows = [r for r in self.rows if r["chunk_id"] in ids]
        if project_key is not None:
            rows = [r for r in rows if r["project_key"] == project_key]
        return rows

    async def fetchrow(self, *args, **kwargs):
        return None


class _Acquire:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def __aenter__(self):
        return _Connection(self._rows)

    async def __aexit__(self, *args):
        return None


class _RegistryPool:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def acquire(self):
        return _Acquire(self._rows)


class RecordingSender:
    """The ONLY fake on the transport side: records outbound text + message id."""

    def __init__(self) -> None:
        self.outbound: list[tuple[str, str]] = []

    async def send_text(self, chat_id: str, text: str) -> str:
        self.outbound.append((chat_id, text))
        return f"provider-e2e-{len(self.outbound)}"

    async def send_media(self, chat_id: str, text: str) -> str:
        raise AssertionError("Zalo ingress must never send media")


class _WebhookStore:
    """Persists what the real route projects, as a row the processor can claim."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self.inserted: list[dict] = []

    async def __call__(self, *, chat_id: str, message_id: str, event_name: str,
                       text: str) -> int | None:
        if any((row["chat_id"], row["message_id"]) ==
               (chat_id, message_id) for row in self.inserted):
            return None
        event_id = len(self.inserted) + 1
        self.inserted.append({"id": event_id, "chat_id": chat_id,
                              "message_id": message_id, "event_name": event_name,
                              "text": text})
        self._store.events[event_id] = MemoryStore.event(event_id, text)
        return event_id


def _wirehook_payload(text: str = QUERY) -> dict:
    """The real Zalo Bot Platform wire form for a PRIVATE text message:
    ``event_name`` at the root, with ``chat`` and ``message_id`` INSIDE
    ``message`` (this is what ``project_zalo_private_text_event`` reads)."""
    return {
        "event_name": "message.text.received",
        "message": {
            "from": {"id": "from-e2e", "display_name": "Tester", "is_bot": False},
            "chat": {"id": CHAT, "chat_type": "PRIVATE"},
            "message_id": "msg-e2e-1",
            "text": text,
            "date": 1750316131602,
        },
    }


def _post(isolated, payload=None, headers=None):
    """POST the real webhook, then wait for the spawned processor task to
    finish: the route only ACKs and spawns, so assertions must not race it."""
    response = isolated.client.post(
        WEBHOOK_PATH,
        json=payload if payload is not None else _wirehook_payload(),
        headers=headers if headers is not None
        else {"X-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )
    isolated.client.portal.call(_drain_tasks)
    return response


async def _drain_tasks() -> None:
    tasks = tuple(zalo_bot_routes._background_tasks)
    for task in tasks:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)
    zalo_bot_routes._background_tasks.clear()


@pytest.fixture
def isolated(monkeypatch):
    """Compose webhook -> processor -> real pipeline -> fake sender."""
    rows = [
        _registry_row(CAMELLIA_CHUNK, CAMELLIA_CONTENT, doc_id="doc-camellia-amenities",
                      project_key="camellia", title="Hồ sơ tiện ích Camellia"),
        _registry_row(SOLEIL_CHUNK, SOLEIL_CONTENT, doc_id="doc-soleil-pool",
                      project_key="soleil", title="Hồ sơ tiện ích Soleil"),
    ]
    monkeypatch.setattr(zalo_bot_routes, "_background_tasks", set())

    # The real registry reads, answered in memory (no PG, no network).
    async def pool():
        return _RegistryPool(rows)

    monkeypatch.setattr("api.application.services.rag_leg.get_ro_pool", pool)
    monkeypatch.setattr("api.application.services.merge.get_ro_pool", pool)

    async def load_record(project_key: str):
        return ProjectRegistryRecord(
            project_key=project_key,
            ten_thuong_mai="Camellia" if project_key == "camellia" else "Soleil",
            ten_phap_ly=None, vi_tri=None, hotline=None, location="Da Nang",
            geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active",
        )

    monkeypatch.setattr(
        "api.application.services.project_config.load_project_registry_record", load_record
    )

    audits: list[dict] = []

    async def record_audit(entry: dict) -> None:
        audits.append(dict(entry))

    monkeypatch.setattr("api.application.pipelines.workflow.write_audit", record_audit)

    # Level B seam install: only the external boundaries are faked.
    harness = LevelBHarness(
        monkeypatch,
        db=None,
        project=SimpleNamespace(project_key="camellia"),
        query=QUERY,
        draft=ANSWER,
        rag_chunks={CAMELLIA_CHUNK: CAMELLIA_CONTENT, SOLEIL_CHUNK: SOLEIL_CONTENT},
        session_id="zalo-e2e",
    )

    # Spy on the REAL class: recording the instance proves the factory built a
    # genuine RagQueryPipelineConv and that its own `run` produced the answer.
    original_run = RagQueryPipelineConv.run
    seen: list[RagQueryPipelineConv] = []

    async def spy_run(self, *args, **kwargs):
        seen.append(self)
        return await original_run(self, *args, **kwargs)

    monkeypatch.setattr(RagQueryPipelineConv, "run", spy_run)

    async def catalogue() -> list[tuple[str, str]]:
        return [("camellia", "Camellia"), ("soleil", "Soleil")]

    history = MemoryHistory()
    core = GlobalAssistantService(
        history=history, pipeline_factory=RagQueryPipelineConv, catalogue_loader=catalogue
    )
    assert core._pipeline_factory is RagQueryPipelineConv, "mock pipeline slipped in"

    store = MemoryStore()
    store.events = {}
    sender = RecordingSender()
    settings = SimpleNamespace(
        zalo_bot_enabled=True, zalo_bot_open_demo_access=False,
        zalo_bot_core_timeout_seconds=90, zalo_bot_lease_seconds=120,
        zalo_bot_lease_margin_seconds=15,
    )
    processor = ZaloBotProcessor(
        store=store, core=core, sender=sender, identity_secret=SECRET,
        admit=DeduplicatingAdmission(), settings=settings,
    )
    # The route's scheduler IS the real processor.
    original_scheduler = zalo_bot_dispatch.get_scheduler()
    zalo_bot_dispatch.set_scheduler(processor.process)

    route_settings = SimpleNamespace(
        zalo_bot_enabled=True, zalo_bot_open_demo_access=False,
        zalo_bot_webhook_secret=SecretStr(WEBHOOK_SECRET),
        zalo_bot_max_body_bytes=65536,
    )
    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: route_settings)
    webhook_store = _WebhookStore(store)
    app = FastAPI()
    app.include_router(zalo_bot_routes.router)
    app.state.settings = route_settings
    app.dependency_overrides[zalo_bot_routes.get_zalo_event_store] = lambda: webhook_store

    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, store=store, sender=sender,
                                  processor=processor, harness=harness, audits=audits,
                                  seen=seen, history=history)
    finally:
        zalo_bot_dispatch.set_scheduler(original_scheduler)
        app.dependency_overrides.clear()
        assert not zalo_bot_routes._background_tasks, "Leaked ingress tasks"


def _seen_types(isolated) -> list[type]:
    """Types actually constructed and run, so a mock pipeline cannot pass."""
    return [type(instance) for instance in isolated.seen]


def test_webhook_drives_real_pipeline_to_a_recorded_outbound(isolated):
    response = _post(isolated)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "accepted": True}

    event = isolated.store.events[1]
    # (a) The REAL pipeline produced a real, non-empty answer.
    assert _seen_types(isolated) == [RagQueryPipelineConv], "pipeline factory built a mock"
    assert isolated.harness.fake_rag.queries, "retrieval never ran"
    assert isolated.harness.prompts, "the real prompt builder never ran"
    assert event.assistant_outcome["resolution_status"] == "resolved"
    assert event.assistant_outcome["project_key"] == "camellia"
    answer = event.assistant_outcome["answer"]
    assert answer.strip() and answer == ANSWER

    # (b) Exactly one outbound left through the send path, with its message id.
    assert isolated.sender.outbound == [(CHAT, answer)]
    assert event.outbound_message_id == "provider-e2e-1"
    assert event.status == "done"

    # The audited bytes ARE the outbound bytes: the answer came out of the real
    # publication gate, not a stubbed core.
    assert isolated.audits[-1]["answer_hash"] == hashlib.sha256(
        answer.encode("utf-8")
    ).hexdigest()

    # (c) No cross-project leak: Soleil's chunk never entered the Camellia turn.
    prompt = isolated.harness.prompt()
    assert CAMELLIA_CONTENT in prompt
    assert SOLEIL_CONTENT not in prompt
    assert SOLEIL_CHUNK not in prompt
    assert set(isolated.audits[-1]["chunk_ids"]) == {CAMELLIA_CHUNK}


def test_duplicate_webhook_never_reaches_the_provider_twice(isolated):
    first = _post(isolated)
    second = _post(isolated)
    assert first.json() == {"ok": True, "accepted": True}
    assert second.json() == {"ok": True, "accepted": False}
    assert len(isolated.sender.outbound) == 1
    assert isolated.store.events[1].outbound_message_id == "provider-e2e-1"


def test_cached_outcome_is_resent_without_a_second_pipeline_run(isolated):
    assert _post(isolated).json() == {"ok": True, "accepted": True}
    audits_after_first = len(isolated.audits)

    # Receipt cleared, cached outcome kept: the crash-before-receipt window.
    # The processor must reuse the cached outcome instead of re-running RAG.
    event = isolated.store.events[1]
    event.outbound_message_id = None
    event.status = "failed"
    isolated.client.portal.call(
        lambda: asyncio.gather(isolated.processor.process(1), return_exceptions=True)
    )
    assert event.status == "done"
    assert len(isolated.sender.outbound) == 2
    assert isolated.sender.outbound[1] == (CHAT, ANSWER)
    assert len(isolated.audits) == audits_after_first, "cached turn re-ran the pipeline"
    assert _seen_types(isolated) == [RagQueryPipelineConv]


def test_unbound_identity_is_rejected_before_the_pipeline(isolated, monkeypatch):
    monkeypatch.setattr(isolated.store, "binding_active", False)
    assert _post(isolated).json() == {"ok": True, "accepted": True}
    assert isolated.seen == [], "pipeline ran for an unauthorized identity"
    assert isolated.sender.outbound == []
    assert isolated.store.events[1].status == "rejected"


def test_wrong_secret_never_reaches_the_pipeline(isolated):
    response = _post(isolated, headers={"X-Bot-Api-Secret-Token": "wrong-secret"})
    assert response.status_code == 403
    assert isolated.seen == []
    assert isolated.sender.outbound == []
    assert isolated.store.events == {}


def test_group_chat_event_never_reaches_the_pipeline(isolated):
    payload = _wirehook_payload()
    payload["message"]["chat"]["chat_type"] = "GROUP"
    assert _post(isolated, payload).json() == {"ok": True, "accepted": False}
    assert isolated.seen == []
    assert isolated.sender.outbound == []


def test_identity_key_binds_the_event_to_its_chat(isolated):
    assert _post(isolated).json() == {"ok": True, "accepted": True}
    event = isolated.store.events[1]
    assert event.identity_key == opaque_identity(CHAT, SECRET)
    assert event.source_event_key == "zalo-bot:event:v1:1"