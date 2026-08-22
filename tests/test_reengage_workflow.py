"""Story 9.4 / ISSUE-10 — ReengageMatchWorkflow + admin trigger (offline).

Fakes stand in for all three ports (lead repository, need-profile embedder,
re-approach queue store) so the whole pipeline runs zero-network. Covers:
per-customer aggregation, hard budget filter, cosine scoring and descending
order, the defense-in-depth marketing-consent gate, the reminder cap across
runs, the full StartEvent->StopEvent run, and the admin route auth matrix via
the local-RSA JWKS pattern from tests/test_admin_auth.py.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from datetime import datetime, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from llama_index.core.workflow import Context

from api.application.pipelines.reengage_workflow import (
    EmbeddedNeedProfile,
    MatchesScoredEvent,
    ProjectActivation,
    ProfilesEmbeddedEvent,
    RejectedCustomerProfile,
    ReengageMatchWorkflow,
    aggregate_rejected_lead_rows_into_profiles,
    build_project_sale_profile_text,
    cosine_similarity_between_vectors,
    passes_hard_budget_filter,
    passes_marketing_consent_gate,
    run_reengage_matching_for_activated_project,
)
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import LeadRow
from api.infrastructure import dependencies as dependency_injection
from api.interfaces.api import deps as admin_deps
from api.interfaces.api.admin_routes import ReengageRunRequest
from api.interfaces.api.main import create_app


SOLEIL_ACTIVATION = ProjectActivation(
    project_key="soleil",
    display_name="Soleil Riverside",
    description="Can ho ven song Sai Gon",
    price_min_vnd=3_000_000_000,
    price_max_vnd=6_000_000_000,
)

_ROW_SEQUENCE = itertools.count(1)


def make_lead_row(
    phone: str,
    *,
    name: str | None = None,
    rejection_reason: str | None = None,
    budget_vnd: int | None = None,
    consent_marketing: bool = True,
    marketing_withdrawn_at: datetime | None = None,
    created_at: datetime | None = None,
) -> LeadRow:
    """A lost lead row carrying only the fields the pipeline actually reads."""
    return LeadRow(
        id=next(_ROW_SEQUENCE),
        session_id=None,
        project_key="camellia",
        device_id=None,
        name=name,
        phone=phone,
        consent=True,
        note=None,
        budget_vnd=budget_vnd,
        created_at=created_at or datetime(2026, 8, 1, tzinfo=timezone.utc),
        status="lost",
        assigned_sales_id=None,
        lock_expires_at=None,
        escal_count=0,
        last_action_at=None,
        closed_at=None,
        rejection_reason=rejection_reason,
        consent_service=True,
        consent_marketing=consent_marketing,
        marketing_withdrawn_at=marketing_withdrawn_at,
    )


class StubLeadRepository:
    """Returns a canned rejected-leads page, as the PG pre-filter would."""

    def __init__(self, rejected_lead_rows: list[LeadRow]) -> None:
        self.rejected_lead_rows = rejected_lead_rows
        self.list_calls = 0

    async def list_marketing_eligible_rejected_leads(self) -> list[LeadRow]:
        self.list_calls += 1
        return self.rejected_lead_rows


class StubNeedProfileEmbedding:
    """Deterministic embedder keyed on text markers so similarity is exact."""

    def __init__(self, vector_by_marker: dict[str, list[float]]) -> None:
        self.vector_by_marker = vector_by_marker
        self.embedded_texts: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)

        def vector_for_text(text: str) -> list[float]:
            for marker, vector in self.vector_by_marker.items():
                if marker in text:
                    return vector
            return [0.0, 0.0]

        return [vector_for_text(text) for text in texts]


class InMemoryReengageQueueStore:
    """Queue store whose attempt counts derive from already-saved entries."""

    def __init__(self) -> None:
        self.saved_entries: list = []

    async def save_queue_entries(self, entries) -> None:
        self.saved_entries.extend(entries)

    async def load_attempt_counts_by_customer_id(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.saved_entries:
            counts[entry.customer_id] = counts.get(entry.customer_id, 0) + 1
        return counts


def build_workflow(
    *,
    lead_rows: list[LeadRow] | None = None,
    embedding: StubNeedProfileEmbedding | None = None,
    queue_store: InMemoryReengageQueueStore | None = None,
    activation: ProjectActivation | None = None,
) -> ReengageMatchWorkflow:
    return ReengageMatchWorkflow(
        project_activation=activation or SOLEIL_ACTIVATION,
        lead_repository=StubLeadRepository(lead_rows or []),
        need_profile_embedding=embedding or StubNeedProfileEmbedding({}),
        reengage_queue_store=queue_store or InMemoryReengageQueueStore(),
    )


# ---------------------------------------------------------------------------
# Pure helper semantics
# ---------------------------------------------------------------------------


def test_aggregate_keeps_newest_row_per_customer() -> None:
    newer_row = make_lead_row(
        "0900000001",
        name="Anh Minh",
        rejection_reason="het ngan sach",
        budget_vnd=5_000_000_000,
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    older_row = make_lead_row(
        "0900000001",
        name="Anh Minh",
        rejection_reason="khong thich vi tri",
        budget_vnd=None,
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    profiles = aggregate_rejected_lead_rows_into_profiles([newer_row, older_row])
    assert len(profiles) == 1
    assert profiles[0].rejection_reason == "het ngan sach"
    assert profiles[0].budget_vnd == 5_000_000_000


def test_cosine_similarity_zero_vector_scores_zero_not_nan() -> None:
    assert cosine_similarity_between_vectors([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity_between_vectors([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_hard_budget_filter_drops_out_of_band_keeps_unknown_budget() -> None:
    profile_in_band = RejectedCustomerProfile(
        customer_id="c1", display_name=None, rejection_reason=None,
        budget_vnd=4_000_000_000, consent_marketing=True,
        marketing_withdrawn_at=None,
    )
    profile_below_band = RejectedCustomerProfile(
        customer_id="c2", display_name=None, rejection_reason=None,
        budget_vnd=2_000_000_000, consent_marketing=True,
        marketing_withdrawn_at=None,
    )
    profile_unknown_budget = RejectedCustomerProfile(
        customer_id="c3", display_name=None, rejection_reason=None,
        budget_vnd=None, consent_marketing=True,
        marketing_withdrawn_at=None,
    )
    assert passes_hard_budget_filter(profile_in_band, SOLEIL_ACTIVATION)
    assert not passes_hard_budget_filter(profile_below_band, SOLEIL_ACTIVATION)
    # No stated budget must not silently exclude the customer.
    assert passes_hard_budget_filter(profile_unknown_budget, SOLEIL_ACTIVATION)


def test_marketing_consent_gate_blocks_opt_out_and_withdrawn() -> None:
    withdrawn_at = datetime(2026, 8, 5, tzinfo=timezone.utc)
    opted_out = RejectedCustomerProfile(
        customer_id="c1", display_name=None, rejection_reason=None,
        budget_vnd=None, consent_marketing=False, marketing_withdrawn_at=None,
    )
    withdrawn = RejectedCustomerProfile(
        customer_id="c2", display_name=None, rejection_reason=None,
        budget_vnd=None, consent_marketing=True,
        marketing_withdrawn_at=withdrawn_at,
    )
    clean = RejectedCustomerProfile(
        customer_id="c3", display_name=None, rejection_reason=None,
        budget_vnd=None, consent_marketing=True, marketing_withdrawn_at=None,
    )
    assert not passes_marketing_consent_gate(opted_out)
    assert not passes_marketing_consent_gate(withdrawn)
    assert passes_marketing_consent_gate(clean)


def test_project_sale_profile_text_carries_price_band() -> None:
    text = build_project_sale_profile_text(SOLEIL_ACTIVATION)
    assert "Soleil Riverside" in text
    # Plain int formatting: no thousands separators in the embedded text.
    assert "3000000000" in text
    assert "6000000000" in text


# ---------------------------------------------------------------------------
# Step-driven behaviour (Context.store seeded, no LlamaIndex run loop)
# ---------------------------------------------------------------------------


def _profile(customer_id: str, **overrides) -> RejectedCustomerProfile:
    defaults = dict(
        display_name=None,
        rejection_reason="ly do",
        budget_vnd=4_000_000_000,
        consent_marketing=True,
        marketing_withdrawn_at=None,
    )
    defaults.update(overrides)
    return RejectedCustomerProfile(customer_id=customer_id, **defaults)


def test_score_step_filters_budget_then_rank_orders_by_similarity_desc() -> None:
    async def go():
        workflow = build_workflow()
        ctx = Context(workflow=workflow)
        await ctx.store.set("project_activation", workflow.project_activation)
        await ctx.store.set(
            "embedded_need_profiles",
            [
                EmbeddedNeedProfile(profile=_profile("cust-low"), need_profile_vector=[0.6, 0.8]),
                EmbeddedNeedProfile(profile=_profile("cust-high"), need_profile_vector=[1.0, 0.0]),
                # Same perfect match but budget below the band: must be dropped.
                EmbeddedNeedProfile(
                    profile=_profile("cust-oob", budget_vnd=1_000_000_000),
                    need_profile_vector=[1.0, 0.0],
                ),
            ],
        )
        await ctx.store.set("project_sale_profile_vector", [1.0, 0.0])

        await workflow.score_step(ctx, ProfilesEmbeddedEvent())
        scored = await ctx.store.get("scored_reengage_candidates")
        assert {candidate.profile.customer_id for candidate in scored} == {
            "cust-low",
            "cust-high",
        }

        await ctx.store.set("attempt_counts_by_customer_id", {})
        await workflow.rank_and_queue_step(ctx, MatchesScoredEvent())
        entries = await ctx.store.get("reengage_queue_entries")
        # Descending similarity: the perfect match outranks the 0.6 one.
        assert [entry.customer_id for entry in entries] == ["cust-high", "cust-low"]
        assert [entry.similarity_score for entry in entries] == [pytest.approx(1.0), pytest.approx(0.6)]
        assert [entry.attempt_count for entry in entries] == [1, 1]

    asyncio.run(go())


def test_rank_and_queue_step_enforces_reminder_cap() -> None:
    async def go():
        workflow = build_workflow()
        ctx = Context(workflow=workflow)
        await ctx.store.set("project_activation", workflow.project_activation)
        await ctx.store.set(
            "embedded_need_profiles",
            [
                EmbeddedNeedProfile(profile=_profile("capped"), need_profile_vector=[1.0, 0.0]),
                EmbeddedNeedProfile(profile=_profile("under-cap"), need_profile_vector=[0.9, 0.1]),
            ],
        )
        await ctx.store.set("project_sale_profile_vector", [1.0, 0.0])
        await workflow.score_step(ctx, ProfilesEmbeddedEvent())
        await ctx.store.set(
            "attempt_counts_by_customer_id",
            {"capped": 3, "under-cap": 2},
        )

        await workflow.rank_and_queue_step(ctx, MatchesScoredEvent())
        entries = await ctx.store.get("reengage_queue_entries")
        assert [entry.customer_id for entry in entries] == ["under-cap"]
        assert entries[0].attempt_count == 3

    asyncio.run(go())


def test_rank_and_queue_step_rechecks_marketing_consent_gate() -> None:
    async def go():
        # The PG pre-filter should never deliver this row, but the Python
        # gate must hold even when one slips through (defense-in-depth).
        workflow = build_workflow()
        ctx = Context(workflow=workflow)
        await ctx.store.set("project_activation", workflow.project_activation)
        await ctx.store.set(
            "embedded_need_profiles",
            [
                EmbeddedNeedProfile(
                    profile=_profile("opted-out", consent_marketing=False),
                    need_profile_vector=[1.0, 0.0],
                ),
            ],
        )
        await ctx.store.set("project_sale_profile_vector", [1.0, 0.0])
        await workflow.score_step(ctx, ProfilesEmbeddedEvent())
        await ctx.store.set("attempt_counts_by_customer_id", {})

        await workflow.rank_and_queue_step(ctx, MatchesScoredEvent())
        entries = await ctx.store.get("reengage_queue_entries")
        assert entries == []

    asyncio.run(go())


# ---------------------------------------------------------------------------
# End-to-end run over the real event chain
# ---------------------------------------------------------------------------

MARKERS = {
    "Soleil Riverside": [1.0, 0.0],  # activated-project sale profile
    "het ngan sach": [1.0, 0.0],     # perfect-need customer
    "khong thich vi tri": [0.6, 0.8],  # moderate-match customer
}


def test_full_run_queues_only_matching_consenting_customers() -> None:
    lead_rows = [
        make_lead_row(
            "0900000001",
            name="Chi Lan",
            rejection_reason="het ngan sach du an cu",
            budget_vnd=4_000_000_000,
        ),
        # PG would pre-filter these two; keeping them here proves the
        # Python-side gates catch what SQL misses.
        make_lead_row(
            "0900000002",
            name="Anh Hung",
            rejection_reason="khong thich vi tri",
            budget_vnd=4_000_000_000,
            consent_marketing=False,
        ),
        make_lead_row(
            "0900000003",
            name="Anh Tuan",
            rejection_reason="qua xa",
            budget_vnd=2_000_000_000,
        ),
        make_lead_row("0900000004", name="Chi Hoa", budget_vnd=None),
    ]
    queue_store = InMemoryReengageQueueStore()
    embedding = StubNeedProfileEmbedding(MARKERS)

    result = asyncio.run(
        run_reengage_matching_for_activated_project(
            SOLEIL_ACTIVATION,
            lead_repository=StubLeadRepository(lead_rows),
            need_profile_embedding=embedding,
            reengage_queue_store=queue_store,
        )
    )

    eligible_ids = {
        get_expected_customer_id(phone)
        for phone in ["0900000001", "0900000004"]  # matched + unknown-budget kept
    }
    assert result["queued_count"] == 2
    assert result["activated_project_key"] == "soleil"
    assert {entry.customer_id for entry in result["entries"]} == eligible_ids
    # Descending similarity puts the perfect match first.
    assert result["entries"][0].similarity_score == pytest.approx(1.0)
    assert all(entry.attempt_count == 1 for entry in result["entries"])
    # One embedder call carries every need text plus the project profile last.
    assert len(embedding.embedded_texts) == len(lead_rows) + 1
    assert len(queue_store.saved_entries) == 2


def get_expected_customer_id(phone: str) -> str:
    from api.application.services.lead_mirror_service import compute_customer_id

    return compute_customer_id(phone)


def test_fourth_run_is_blocked_by_reminder_cap() -> None:
    lead_rows = [
        make_lead_row(
            "0900000009",
            name="Anh Kiet",
            rejection_reason="het ngan sach",
            budget_vnd=4_000_000_000,
        )
    ]
    queue_store = InMemoryReengageQueueStore()

    for expected_attempts in (1, 2, 3):
        result = asyncio.run(
            run_reengage_matching_for_activated_project(
                SOLEIL_ACTIVATION,
                lead_repository=StubLeadRepository(lead_rows),
                need_profile_embedding=StubNeedProfileEmbedding(MARKERS),
                reengage_queue_store=queue_store,
            )
        )
        assert result["queued_count"] == 1
        assert result["entries"][0].attempt_count == expected_attempts

    final_result = asyncio.run(
        run_reengage_matching_for_activated_project(
            SOLEIL_ACTIVATION,
            lead_repository=StubLeadRepository(lead_rows),
            need_profile_embedding=StubNeedProfileEmbedding(MARKERS),
            reengage_queue_store=queue_store,
        )
    )
    assert final_result["queued_count"] == 0


# ---------------------------------------------------------------------------
# Admin trigger route: /api/admin/projects/{key}/reengage-run
# ---------------------------------------------------------------------------

PROJECT_ID = "sale-chat-bot-11e49"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"


@pytest.fixture(scope="module")
def local_rsa_jwk() -> dict:
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-test-key"
    jwk_entry["_private_key"] = private_key
    return jwk_entry


@pytest.fixture(autouse=True)
def offline_auth_seams(monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict) -> None:
    """Same local-JWKS swap as tests/test_admin_auth.py — never touch network."""
    async def fake_key_for_kid(kid: str):
        if kid != local_rsa_jwk["kid"]:
            return None
        jwk_entry = {k: v for k, v in local_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

    from api.infrastructure.adapters import firebase_auth_jwks

    verifier_instance = firebase_auth_jwks.FirebaseAuthJwksVerifier(
        project_id=PROJECT_ID,
        jwks_url="https://example.invalid/jwks",
        issuer=ISSUER,
        audience=PROJECT_ID,
    )
    verifier_instance._key_for_kid = fake_key_for_kid  # type: ignore[method-assign]
    monkeypatch.setattr(
        dependency_injection, "get_firebase_auth_verifier", lambda: verifier_instance
    )

    def fake_fetch_active_sales_id_sync(firebase_uid: str) -> int | None:
        return {"uid-sales-mapped": 777}.get(firebase_uid)

    monkeypatch.setattr(
        admin_deps.admin, "_fetch_active_sales_id_sync", fake_fetch_active_sales_id_sync
    )


def _mint_id_token(local_rsa_jwk: dict, claims: dict) -> str:
    return jwt.encode(
        claims,
        key=local_rsa_jwk["_private_key"],
        algorithm="RS256",
        headers={"kid": local_rsa_jwk["kid"]},
    )


def _base_claims(firebase_uid: str, role: str | None) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": PROJECT_ID,
        "sub": firebase_uid,
        "user_id": firebase_uid,
        "email": f"{firebase_uid}@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
        "firebase": {"sign_in_provider": "password"},
    }
    if role is not None:
        claims["role"] = role
    return claims


def _pipeline_seams(monkeypatch: pytest.MonkeyPatch, lead_rows: list[LeadRow]):
    """Point the route's direct factory calls at the offline stubs."""
    from api.interfaces.api import admin_routes

    queue_store = InMemoryReengageQueueStore()
    embedding = StubNeedProfileEmbedding(MARKERS)

    async def fake_lead_repository():
        return StubLeadRepository(lead_rows)

    async def fake_embedding():
        return embedding

    async def fake_queue_store():
        return queue_store

    monkeypatch.setattr(admin_routes, "get_lead_repository", fake_lead_repository)
    monkeypatch.setattr(admin_routes, "get_need_profile_embedding", fake_embedding)
    monkeypatch.setattr(admin_routes, "get_reengage_queue_store", fake_queue_store)
    return queue_store, embedding


REENGAGE_URL = "/api/admin/projects/soleil/reengage-run"


def test_reengage_run_requires_bearer_token() -> None:
    response = TestClient(create_app()).post(
        REENGAGE_URL, json={"display_name": "Soleil Riverside"}
    )
    assert response.status_code == 401, response.text


def test_reengage_run_forbidden_for_sales_role(
    local_rsa_jwk: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _mint_id_token(local_rsa_jwk, _base_claims("uid-sales-1", "sales"))
    response = TestClient(create_app()).post(
        REENGAGE_URL,
        json={"display_name": "Soleil Riverside"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, response.text


def test_reengage_run_admin_trigger_runs_pipeline(
    local_rsa_jwk: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead_rows = [
        make_lead_row(
            "0900000001",
            name="Chi Lan",
            rejection_reason="het ngan sach du an cu",
            budget_vnd=4_000_000_000,
        ),
        make_lead_row(
            "0900000002",
            name="Anh Tuan",
            rejection_reason="qua xa",
            budget_vnd=2_000_000_000,
        ),
    ]
    queue_store, embedding = _pipeline_seams(monkeypatch, lead_rows)

    token = _mint_id_token(local_rsa_jwk, _base_claims("uid-admin-1", "admin"))
    request_body = ReengageRunRequest(
        display_name="Soleil Riverside",
        description="Can ho ven song Sai Gon",
        price_min_vnd=3_000_000_000,
        price_max_vnd=6_000_000_000,
    )
    response = TestClient(create_app()).post(
        REENGAGE_URL,
        json=request_body.model_dump(),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["queued_count"] == 1
    assert payload["activated_project_key"] == "soleil"
    entry = payload["entries"][0]
    assert entry["customer_id"] == get_expected_customer_id("0900000001")
    assert entry["project_key"] == "soleil"
    assert entry["budget_vnd"] == 4_000_000_000
    assert entry["attempt_count"] == 1
    assert len(queue_store.saved_entries) == 1
    # The route body reached the sale-profile text (embedder saw the project).
    assert any("Soleil Riverside" in text for text in embedding.embedded_texts)


def test_reengage_run_reports_unconfigured_embedding_as_503(
    local_rsa_jwk: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.application.ports.embedding import NeedProfileEmbeddingNotConfiguredError
    from api.interfaces.api import admin_routes

    async def raise_not_configured():
        raise NeedProfileEmbeddingNotConfiguredError("EMBEDDING_API_KEY missing")

    monkeypatch.setattr(admin_routes, "get_need_profile_embedding", raise_not_configured)

    token = _mint_id_token(local_rsa_jwk, _base_claims("uid-admin-1", "admin"))
    response = TestClient(create_app()).post(
        REENGAGE_URL,
        json={"display_name": "Soleil Riverside"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503, response.text
