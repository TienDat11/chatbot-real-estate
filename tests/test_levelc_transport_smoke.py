"""Level C — real transport smoke: the real ASGI app over the disposable registry.

Level B (tests/test_levelb_workflow_proof.py) proves the publication chain
in-process. Level C boots the REAL app (``create_app()``) and drives it over
real HTTP so routing, auth dependencies, the quota gate, global-assistant
project resolution, chat-history persistence, the Zalo ingress store and the
``query_audit`` INSERT are all decided by production code against the same
disposable PostgreSQL registry.

Seam discipline is identical to Level B: only the workflow seams
``LevelBHarness`` installs plus the LightRAG seam are faked.
``merge_module.get_ro_pool``, ``workflow_module.write_audit`` and
``project_config.load_project_registry_record`` stay REAL, so registry
hydration, the training preflight project read and the audit write are
exercised rather than simulated.

Run:
    LEVELB_TEST_PG_ADMIN=postgresql://ragre:ragre_dev_password@127.0.0.1:5432/postgres \
        .venv/Scripts/python.exe -B -m pytest -q tests/test_levelc_transport_smoke.py -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.application.services import anon_identity, zalo_bot_dispatch
from api.infrastructure.adapters import postgres_zalo_events
from api.infrastructure.config.config import Settings
from api.interfaces.api import assistant_routes, zalo_bot_routes
from api.interfaces.api import main as main_module
from api.interfaces.api.main import create_app
from tests._auth_seams import local_rsa_jwk, offline_auth_seams, sales_bearer_headers
from tests._training_seams import training_payload
from tests.levelb_registry_harness import (
    LevelBHarness,
    levelb_registry,  # noqa: F401  (fixture import)
    opened_window,
    registry,
    seed_registry,
    sha256_hex,
    soleil_context,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SOLEIL_DOC = "levelc-soleil-profile"
SOLEIL_CHUNK = "levelc-soleil-profile:1:0"
CAMELLIA_DOC = "levelc-camellia-profile"
CAMELLIA_CHUNK = "levelc-camellia-profile:1:0"
SOLEIL_CONTENT = "Soleil nằm ven biển với hồ bơi chung và vườn cảnh trên cao."
CAMELLIA_CONTENT = "Camellia có vườn cây xanh và lối dạo bộ cho cư dân."
SOLEIL_DRAFT = "Soleil có hồ bơi chung và vườn cảnh trên cao theo hồ sơ dự án."
# Five whitespace-separated words keeps conv slot extraction deterministic
# (>= 6 words would call the real LLM filler).
SOLEIL_QUERY = "Soleil có tiện ích gì"
# Names no project, so the global assistant must stay unresolved.
NEUTRAL_QUERY = "Cho tôi hỏi về tiện ích"
DEVICE_ID = "levelc-device"

WEBHOOK_SECRET = "levelc-webhook-secret"
IDENTITY_SECRET = "levelc-identity-secret-0123456789abcdefghij"
ZALO_PAYLOAD = {
    "ok": True,
    "result": {
        "event_name": "message.text.received",
        "message": {
            "from": {"id": "from-789", "display_name": "Tester", "is_bot": False},
            "chat": {"id": "chat-123", "chat_type": "PRIVATE"},
            "text": "xin gia",
            "message_id": "msg-456",
            "date": 1750316131602,
        },
    },
}

# db/schema.sql + db/audit.sql (everything the Level-B fixture applies) do not
# carry the tables the real transport paths write: chat history (global +
# training), sales/leads (FK targets) and the Zalo ingress tables. They are
# applied here, in dependency order, so the smoke exercises the real
# persistence code instead of a fake repository.
_EXTRA_DDL = (
    "db/lead_schema.sql",
    "db/migrations/2026-08-25-chat-session-history.sql",
    "db/migrations/2026-08-26-chat-sessions-upgrade.sql",
    "db/migrations/2026-08-28-unified-sales-workspace.sql",
    "db/migrations/2026-09-14-global-assistant-sessions.sql",
    "db/migrations/2026-09-17-zalo-bot-events.sql",
    "db/migrations/2026-09-17-zalo-bot-roundtrip.sql",
    "db/migrations/2026-09-17-zalo-bot-hardening.sql",
    "db/migrations/2026-09-17-zalo-bot-hardening-indexes.sql",
    "db/migrations/2026-09-17-zalo-bot-hardening-finalize.sql",
)


def _apply_ddl(cur, relative: str) -> None:
    sql = (REPO_ROOT / relative).read_text(encoding="utf-8")
    if relative.endswith("-indexes.sql"):
        # CREATE INDEX CONCURRENTLY cannot run inside a transaction block: keep
        # the dollar-quoted preflight as one command, then one per index.
        preflight, separator, indexes = sql.partition("$preflight$;")
        assert separator, relative
        cur.execute(preflight + separator)
        for statement in indexes.split(";"):
            if statement.strip():
                cur.execute(statement)
        return
    cur.execute(sql)


@pytest.fixture(scope="module")
def levelc_registry(levelb_registry):
    """Disposable registry, real persistence DDL, and seeded Soleil/Camellia rows."""
    db = levelb_registry
    with registry(db) as cur:
        for relative in _EXTRA_DDL:
            _apply_ddl(cur, relative)
        start, end = opened_window(db["today"])
        seed_registry(
            cur, doc_id=SOLEIL_DOC, chunk_id=SOLEIL_CHUNK, content=SOLEIL_CONTENT,
            project_key="soleil", title="Hồ sơ Soleil", effective_from=start, effective_to=end,
        )
        seed_registry(
            cur, doc_id=CAMELLIA_DOC, chunk_id=CAMELLIA_CHUNK, content=CAMELLIA_CONTENT,
            project_key="camellia", title="Hồ sơ Camellia", effective_from=start, effective_to=end,
        )
    return db


@pytest.fixture
def smoke(levelc_registry, monkeypatch):
    """Level-B seams installed, real ASGI client live (portal kept for task drain)."""
    harness = LevelBHarness(
        monkeypatch,
        db=levelc_registry,
        project=soleil_context(),
        query=SOLEIL_QUERY,
        draft=SOLEIL_DRAFT,
        rag_chunks={SOLEIL_CHUNK: SOLEIL_CONTENT, CAMELLIA_CHUNK: CAMELLIA_CONTENT},
        session_id="levelc-harness",
    )
    # Prewarm is best-effort background work against the fake RAG seam; skip it
    # so the smoke log stays readable.
    monkeypatch.setattr(main_module, "_maybe_start_rag_prewarm", lambda: None)
    # The lifespan installs a Postgres rate-limit port globally; put the
    # process-wide port back so later modules keep their own expectations.
    previous_port = anon_identity.get_rate_limit_port()
    try:
        # The real rate-limit port persists the client address into an INET
        # column, so the client needs a real peer IP (its default is the
        # literal string "testclient").
        with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
            yield harness, client
    finally:
        anon_identity.set_rate_limit_port(previous_port)


class _QuotaProbe:
    """Count gate reservations without changing any decision the gate makes."""

    def __init__(self) -> None:
        self.gates = 0
        self.prepared = 0


@pytest.fixture
def quota_probe(smoke, monkeypatch):
    _, client = smoke
    probe = _QuotaProbe()
    original = assistant_routes.build_query_quota_gate

    def build():
        probe.gates += 1
        gate = original()
        inner = gate.prepare_turn

        async def prepare_turn(**kwargs):
            probe.prepared += 1
            return await inner(**kwargs)

        gate.prepare_turn = prepare_turn
        return gate

    # JSON route: module attribute. SSE route: Depends captured the original
    # function object at import time, so override that key too.
    monkeypatch.setattr(assistant_routes, "build_query_quota_gate", build)
    client.app.dependency_overrides[original] = build
    return probe


def _sse_frames(body: str) -> list[tuple[str, dict]]:
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines["event"], json.loads(lines.get("data", "{}"))))
    return frames


async def _drain_background_tasks() -> None:
    while zalo_bot_routes._background_tasks:
        await asyncio.gather(
            *tuple(zalo_bot_routes._background_tasks), return_exceptions=True
        )


def _scalar(cur, sql: str, params: tuple = ()) -> object:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _audit_count(db) -> int:
    with registry(db) as cur:
        return _scalar(cur, "SELECT count(*) FROM query_audit")


def test_lc1_query_json_publishes_seeded_project_scoped_answer(smoke):
    """C1 — /query JSON answers only from the seeded Soleil registry rows."""
    harness, client = smoke
    response = client.post(
        "/query",
        json={
            "query": SOLEIL_QUERY,
            "project_key": "soleil",
            "session_id": "lc-json-1",
            "device_id": DEVICE_ID,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["answer"] == SOLEIL_DRAFT
    assert [source["doc_id"] for source in body["sources"]] == [SOLEIL_DOC]
    assert body["facts"] == []
    assert body["history_persisted"] is True
    assert "Camellia" not in json.dumps(body, ensure_ascii=False)

    row = harness.audit_row(body["trace_id"])
    assert row is not None
    assert row["chunk_ids"] == [SOLEIL_CHUNK]

    with registry(harness.db) as cur:
        cur.execute(
            "SELECT DISTINCT project_key FROM chat_messages WHERE session_id=%s",
            ("lc-json-1",),
        )
        assert [project for (project,) in cur.fetchall()] == ["soleil"]


def test_lc2_query_sse_terminal_bytes_match_the_json_transport(smoke):
    """C2 — SSE framing ends with the same answer bytes the JSON path returns."""
    _, client = smoke
    payload = {
        "query": SOLEIL_QUERY, "project_key": "soleil", "device_id": DEVICE_ID,
    }
    expected = client.post(
        "/query", json={**payload, "session_id": "lc-sse-json"}
    ).json()
    response = client.post(
        "/query",
        json={**payload, "session_id": "lc-sse-stream"},
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    frames = _sse_frames(response.text)
    names = [name for name, _ in frames]
    assert names[0] == "ack"
    assert names[-1] == "done"
    assert "token" in names

    streamed = "".join(
        data.get("text", "") for name, data in frames if name == "token"
    )
    done = frames[-1][1]
    assert streamed == done["answer"] == expected["answer"] == SOLEIL_DRAFT
    assert done["sources"] == expected["sources"]
    assert done["status"] == "completed"


def test_lc3_ga_json_unresolved_turn_runs_no_pipeline_and_no_quota(
    smoke, quota_probe, local_rsa_jwk, offline_auth_seams
):
    """C3a — an unresolved GA turn is a clarification: no pipeline, no quota."""
    harness, client = smoke
    before = _audit_count(harness.db)
    response = client.post(
        "/api/assistant/query",
        headers={
            **sales_bearer_headers(local_rsa_jwk),
            "X-Device-Id": DEVICE_ID,
            "x-session-id": "lc-ga-neutral",
        },
        json={"query": NEUTRAL_QUERY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resolution_status"] == "unresolved"
    assert body["project_key"] is None
    assert body["rag_ran"] is False
    assert body["replayed"] is False
    assert set(body["candidates"]) == {"camellia", "soleil"}
    assert "camellia, soleil" in body["answer"]

    assert harness.fake_rag.queries == []
    assert harness.prompts == []
    assert quota_probe.prepared == 0
    assert _audit_count(harness.db) == before

    # The clarification still lands durably, scoped to the global container.
    with registry(harness.db) as cur:
        assert _scalar(
            cur,
            "SELECT session_mode FROM chat_sessions WHERE session_id=%s",
            ("lc-ga-neutral",),
        ) == "global"
        assert _scalar(
            cur,
            "SELECT active_project_key FROM chat_sessions WHERE session_id=%s",
            ("lc-ga-neutral",),
        ) is None


def test_lc4_ga_json_resolved_turn_publishes(
    smoke, quota_probe, local_rsa_jwk, offline_auth_seams
):
    """C3b — a resolved GA turn publishes through the real registry + audit."""
    harness, client = smoke
    response = client.post(
        "/api/assistant/query",
        headers={
            **sales_bearer_headers(local_rsa_jwk),
            "X-Device-Id": DEVICE_ID,
            "x-session-id": "lc-ga-resolved",
        },
        json={"query": SOLEIL_QUERY, "selected_project_key": "soleil"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resolution_status"] == "resolved"
    assert body["project_key"] == "soleil"
    assert body["rag_ran"] is True
    assert body["answer"] == SOLEIL_DRAFT
    assert [source["doc_id"] for source in body["meta"]["sources"]] == [SOLEIL_DOC]
    assert quota_probe.prepared == 1

    row = harness.audit_row(body["trace_id"])
    assert row is not None
    assert row["answer_hash"] == sha256_hex(body["answer"])
    assert row["chunk_ids"] == [SOLEIL_CHUNK]

    with registry(harness.db) as cur:
        assert _scalar(
            cur,
            "SELECT active_project_key FROM chat_sessions WHERE session_id=%s",
            ("lc-ga-resolved",),
        ) == "soleil"
        cur.execute(
            "SELECT DISTINCT project_key FROM chat_messages WHERE session_id=%s",
            ("lc-ga-resolved",),
        )
        assert [row_[0] for row_ in cur.fetchall()] == ["soleil"]


def test_lc5_ga_stream_unresolved_emits_ack_project_context_done(
    smoke, quota_probe, local_rsa_jwk, offline_auth_seams
):
    """C4 — GA SSE ordering, and no token frame for an unresolved turn."""
    harness, client = smoke
    response = client.post(
        "/api/assistant/query/stream",
        headers={
            **sales_bearer_headers(local_rsa_jwk),
            "X-Device-Id": DEVICE_ID,
            "x-session-id": "lc-ga-stream",
            "Accept": "text/event-stream",
        },
        json={"query": NEUTRAL_QUERY},
    )
    assert response.status_code == 200
    frames = _sse_frames(response.text)
    assert [name for name, _ in frames] == ["ack", "project_context", "done"]

    context = frames[1][1]
    assert context["project_key"] is None
    assert context["resolution_status"] == "unresolved"
    assert set(context["candidates"]) == {"camellia", "soleil"}
    assert frames[-1][1]["rag_ran"] is False
    assert harness.fake_rag.queries == []
    assert quota_probe.prepared == 0


def test_lc6_training_publishes_under_its_owner_and_refuses_a_foreign_one(
    smoke, local_rsa_jwk, offline_auth_seams
):
    """C5 — training mode binds the real owner; another sales uid is refused."""
    harness, client = smoke
    session_id = "lc-training-1"
    payload = training_payload(
        query=SOLEIL_QUERY, session_id=session_id, context={"project_key": "soleil"}
    )

    accepted = client.post(
        "/query", json=payload, headers=sales_bearer_headers(local_rsa_jwk)
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["training_session_id"] == session_id
    assert body["context"] == {"project_key": "soleil"}
    assert body["lead_cta_hint"] is None
    assert body["project_redirect"] is None
    assert body["answer"] == SOLEIL_DRAFT
    assert body["history_persisted"] is True
    with registry(harness.db) as cur:
        cur.execute(
            "SELECT owner_firebase_uid, context_project_key, answer_mode"
            " FROM chat_sessions WHERE session_id=%s",
            (session_id,),
        )
        assert cur.fetchall() == [("uid-sales-mapped", "soleil", "training")]

    rag_before = list(harness.fake_rag.queries)
    denied = client.post(
        "/query",
        json=payload,
        headers=sales_bearer_headers(local_rsa_jwk, uid="uid-sales"),
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Training session belongs to another account"
    assert harness.fake_rag.queries == rag_before


def test_lc7_zalo_webhook_replay_persists_one_event_and_dispatches_once(
    smoke, monkeypatch
):
    """C6 — a replayed Zalo webhook is deduplicated by the real ingress store."""
    harness, client = smoke
    settings = Settings(
        _env_file=None,
        app_env="test",
        enforce_approved_llm_provider=False,
        zalo_bot_enabled=True,
        zalo_bot_webhook_secret=SecretStr(WEBHOOK_SECRET),
        zalo_bot_identity_secret=SecretStr(IDENTITY_SECRET),
        zalo_bot_token="levelc-zalo-token",
    )
    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(postgres_zalo_events, "get_settings", lambda: settings)

    dispatched: list[int] = []

    async def recording_scheduler(event_id: int) -> None:
        dispatched.append(event_id)

    monkeypatch.setattr(zalo_bot_dispatch, "_scheduler", recording_scheduler)

    headers = {"X-Bot-Api-Secret-Token": WEBHOOK_SECRET}
    first = client.post(
        "/api/integrations/zalo-bot/webhook", json=ZALO_PAYLOAD, headers=headers
    )
    replay = client.post(
        "/api/integrations/zalo-bot/webhook", json=ZALO_PAYLOAD, headers=headers
    )
    assert first.status_code == 200 and first.json() == {"ok": True, "accepted": True}
    assert replay.status_code == 200 and replay.json() == {"ok": True, "accepted": False}

    client.portal.call(_drain_background_tasks)
    assert len(dispatched) == 1

    with registry(harness.db) as cur:
        cur.execute(
            "SELECT id, chat_id, message_id, status FROM zalo_bot_events"
            " WHERE message_id=%s",
            (ZALO_PAYLOAD["result"]["message"]["message_id"],),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == dispatched[0]
        assert rows[0][1:] == ("chat-123", "msg-456", "pending")
        assert _scalar(
            cur,
            "SELECT count(*) FROM zalo_bot_ingress_keys WHERE event_id=%s",
            (dispatched[0],),
        ) == 1


def test_lc8_published_answer_matches_the_real_audit_row(smoke):
    """C7 — publication integrity: hash, chunk ids, prompt, money, transcript."""
    harness, client = smoke
    body = client.post(
        "/query",
        json={
            "query": SOLEIL_QUERY,
            "project_key": "soleil",
            "session_id": "lc-integrity",
            "device_id": DEVICE_ID,
        },
    ).json()
    answer = body["answer"]

    row = harness.audit_row(body["trace_id"])
    assert row is not None
    assert row["answer_hash"] == sha256_hex(answer)
    assert row["chunk_ids"] == [SOLEIL_CHUNK]
    assert row["guard_verdicts"]["selected_prompt_chunk_ids"] == [SOLEIL_CHUNK]
    assert "camellia" not in json.dumps(row, ensure_ascii=False).lower()

    prompt = harness.prompt()
    assert SOLEIL_CONTENT in prompt
    assert CAMELLIA_CONTENT not in prompt
    # No seeded fact supports a price, so none may reach the answer.
    assert re.search(r"\d+([.,]\d+)?\s*(tỷ|triệu|tr\b|vnd|vnđ|đ)", answer, re.I) is None

    with registry(harness.db) as cur:
        cur.execute(
            "SELECT DISTINCT project_key FROM chat_messages WHERE session_id=%s",
            ("lc-integrity",),
        )
        assert [project for (project,) in cur.fetchall()] == ["soleil"]