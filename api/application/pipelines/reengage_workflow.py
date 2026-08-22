"""ReengageMatchWorkflow — re-approach matching on the LlamaIndex spine (D6).

Story 9.4 / ISSUE-10. When a project is (re)activated, this workflow matches
previously-REJECTED customers to the new offer and enqueues re-approach
suggestions for the CRM dashboard. It follows the same Event/Step pattern as
RagQueryWorkflow (AD-18): events are routing signals, cross-step data lives in
ctx.store, and every step is independently runnable in tests with a partial
store.

Flow: load_rejected_customers_step -> embed_need_profile_step -> score_step ->
rank_and_queue_step -> notify_step.

Two hard gates protect the customer:
1. MARKETING CONSENT — a lead enters the pipeline only when
   consent_marketing is true AND marketing_withdrawn_at is NULL. The PG query
   pre-filters; rank_and_queue_step re-checks in Python (defense-in-depth, and
   it keeps the gate enforced even when steps are driven directly).
2. REMINDER CAP — at most MAX_REENGAGE_ATTEMPTS_PER_CUSTOMER queue entries
   per customer across all projects, counted from the existing queue.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)

from api.application.ports.embedding import NeedProfileEmbeddingPort
from api.application.ports.reengage_queue import ReengageQueueEntry, ReengageQueueStore
from api.infrastructure.ports.leads import LeadRepository, LeadRow

logger = logging.getLogger("api.pipelines.reengage_workflow")

# Reminder cap per customer ("không spam"): after this many queued re-approach
# suggestions a customer is never matched again.
MAX_REENGAGE_ATTEMPTS_PER_CUSTOMER = 3


@dataclass(frozen=True)
class ProjectActivation:
    """The activated-project trigger (later emitted by PublishProjectWorkflow).

    Carries everything matching needs about the new offer so ISSUE-13 can fire
    this workflow without an extra registry round-trip.
    """

    project_key: str
    display_name: str
    description: str = ""
    # Inclusive sale price band (VNĐ); None disables the hard budget filter.
    price_min_vnd: int | None = None
    price_max_vnd: int | None = None


@dataclass(frozen=True)
class RejectedCustomerProfile:
    """One rejected customer aggregated from their lost lead rows."""

    customer_id: str
    display_name: str | None
    rejection_reason: str | None
    budget_vnd: int | None
    consent_marketing: bool
    marketing_withdrawn_at: datetime | None


@dataclass(frozen=True)
class ScoredReengageCandidate:
    """A consent-clean customer whose need profile matches the activation."""

    profile: RejectedCustomerProfile
    similarity_score: float


@dataclass(frozen=True)
class EmbeddedNeedProfile:
    profile: RejectedCustomerProfile
    need_profile_vector: list[float] = field(default_factory=list)


# --- Workflow events (signals only, mirroring RagQueryWorkflow style) ---------
class CustomersLoadedEvent(Event):
    pass


class ProfilesEmbeddedEvent(Event):
    pass


class MatchesScoredEvent(Event):
    pass


class QueueUpdatedEvent(Event):
    pass


def build_customer_need_profile_text(profile: RejectedCustomerProfile) -> str:
    """Vietnamese need-profile text fed to the embedder.

    Kept short and field-based (no free-form generation): the embedding only
    needs the customer's rejection context, budget signal, and prior interest.
    """
    parts = ["Khách hàng từng từ chối dự án"]
    if profile.rejection_reason:
        parts.append(f"Lý do từ chối: {profile.rejection_reason}")
    if profile.budget_vnd is not None:
        parts.append(f"Ngân sách khoảng {profile.budget_vnd} VNĐ")
    return ". ".join(parts)


def build_project_sale_profile_text(activation: ProjectActivation) -> str:
    """Sale-profile text for the activated project, same space as need profiles."""
    parts = [f"Dự án {activation.display_name} vừa mở bán"]
    if activation.description:
        parts.append(activation.description)
    if activation.price_min_vnd is not None or activation.price_max_vnd is not None:
        low = activation.price_min_vnd or 0
        high = activation.price_max_vnd or "trở lên"
        parts.append(f"Tầm giá từ {low} đến {high} VNĐ")
    return ". ".join(parts)


def cosine_similarity_between_vectors(
    left_vector: list[float], right_vector: list[float]
) -> float:
    """Plain cosine similarity; zero-norm vectors score 0 instead of NaN."""
    dot_product = sum(a * b for a, b in zip(left_vector, right_vector))
    left_norm = math.sqrt(sum(a * a for a in left_vector))
    right_norm = math.sqrt(sum(b * b for b in right_vector))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)


def aggregate_rejected_lead_rows_into_profiles(
    rejected_lead_rows: list[LeadRow],
) -> list[RejectedCustomerProfile]:
    """Group lost leads into one profile per customer (customer_id = HMAC phone).

    The most recent row wins per field so the freshest reason/budget drives
    matching. Consent fields come from the effective columns with the legacy
    fallback semantics used everywhere else (None consent -> not eligible).
    """
    from api.application.services.lead_mirror_service import compute_customer_id

    profiles_by_customer_id: dict[str, RejectedCustomerProfile] = {}
    for lead_row in rejected_lead_rows:  # rows arrive newest-first
        customer_id = compute_customer_id(lead_row.phone)
        if customer_id in profiles_by_customer_id:
            continue
        profiles_by_customer_id[customer_id] = RejectedCustomerProfile(
            customer_id=customer_id,
            display_name=lead_row.name,
            rejection_reason=lead_row.rejection_reason,
            budget_vnd=lead_row.budget_vnd,
            consent_marketing=bool(lead_row.consent_marketing),
            marketing_withdrawn_at=lead_row.marketing_withdrawn_at,
        )
    return list(profiles_by_customer_id.values())


def passes_hard_budget_filter(
    profile: RejectedCustomerProfile, activation: ProjectActivation
) -> bool:
    """Hard price-band filter: an out-of-band budget drops the candidate.

    A customer without a stated budget is kept — absence of information must
    not silently exclude them; the sales still makes the call.
    """
    if profile.budget_vnd is None:
        return True
    if activation.price_min_vnd is not None and profile.budget_vnd < activation.price_min_vnd:
        return False
    if activation.price_max_vnd is not None and profile.budget_vnd > activation.price_max_vnd:
        return False
    return True


def passes_marketing_consent_gate(profile: RejectedCustomerProfile) -> bool:
    """The consent gate re-check: opt-in marketing AND no recorded withdrawal."""
    return profile.consent_marketing and profile.marketing_withdrawn_at is None


class ReengageMatchWorkflow(Workflow):
    """D6-spine workflow matching rejected customers to a newly activated project."""

    def __init__(
        self,
        *,
        project_activation: ProjectActivation,
        lead_repository: LeadRepository,
        need_profile_embedding: NeedProfileEmbeddingPort,
        reengage_queue_store: ReengageQueueStore,
        max_reengage_attempts_per_customer: int = MAX_REENGAGE_ATTEMPTS_PER_CUSTOMER,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.project_activation = project_activation
        self.lead_repository = lead_repository
        self.need_profile_embedding = need_profile_embedding
        self.reengage_queue_store = reengage_queue_store
        self.max_reengage_attempts_per_customer = max_reengage_attempts_per_customer

    async def _store_get(self, ctx: Context, key: str):
        """Read one store key as absent (not error) when steps run standalone."""
        try:
            return await ctx.store.get(key)
        except ValueError:  # documented "Path not found in state" miss
            return None

    @step()
    async def load_rejected_customers_step(
        self, ctx: Context, ev: StartEvent
    ) -> CustomersLoadedEvent:
        await ctx.store.set("project_activation", self.project_activation)

        attempt_counts = await self.reengage_queue_store.load_attempt_counts_by_customer_id()
        await ctx.store.set("attempt_counts_by_customer_id", attempt_counts)

        # PG pre-filter already applied the consent gate; aggregation keeps one
        # fresh profile per customer regardless of how many projects they lost.
        rejected_lead_rows = await self.lead_repository.list_marketing_eligible_rejected_leads()
        profiles = aggregate_rejected_lead_rows_into_profiles(rejected_lead_rows)
        await ctx.store.set("rejected_customer_profiles", profiles)
        return CustomersLoadedEvent()

    @step()
    async def embed_need_profile_step(
        self, ctx: Context, ev: CustomersLoadedEvent
    ) -> ProfilesEmbeddedEvent:
        profiles: list[RejectedCustomerProfile] = await ctx.store.get(
            "rejected_customer_profiles"
        )
        activation: ProjectActivation = await ctx.store.get("project_activation")

        need_texts = [build_customer_need_profile_text(profile) for profile in profiles]
        all_vectors = await self.need_profile_embedding.embed_texts(
            [*need_texts, build_project_sale_profile_text(activation)]
        )
        project_sale_profile_vector = all_vectors[-1]
        embedded_profiles = [
            EmbeddedNeedProfile(profile=profile, need_profile_vector=all_vectors[index])
            for index, profile in enumerate(profiles)
        ]
        await ctx.store.set("embedded_need_profiles", embedded_profiles)
        await ctx.store.set("project_sale_profile_vector", project_sale_profile_vector)
        return ProfilesEmbeddedEvent()

    @step()
    async def score_step(self, ctx: Context, ev: ProfilesEmbeddedEvent) -> MatchesScoredEvent:
        embedded_profiles: list[EmbeddedNeedProfile] = await ctx.store.get(
            "embedded_need_profiles"
        )
        activation: ProjectActivation = await self._store_get(ctx, "project_activation")
        project_vector: list[float] = await ctx.store.get("project_sale_profile_vector")

        scored_candidates = [
            ScoredReengageCandidate(
                profile=embedded.profile,
                similarity_score=cosine_similarity_between_vectors(
                    embedded.need_profile_vector, project_vector
                ),
            )
            for embedded in embedded_profiles
            if passes_hard_budget_filter(embedded.profile, activation)
        ]
        await ctx.store.set("scored_reengage_candidates", scored_candidates)
        return MatchesScoredEvent()

    @step()
    async def rank_and_queue_step(self, ctx: Context, ev: MatchesScoredEvent) -> QueueUpdatedEvent:
        scored_candidates: list[ScoredReengageCandidate] = await ctx.store.get(
            "scored_reengage_candidates"
        )
        activation: ProjectActivation = await ctx.store.get("project_activation")
        attempt_counts: dict[str, int] = await ctx.store.get("attempt_counts_by_customer_id")

        eligible_candidates = [
            candidate
            for candidate in scored_candidates
            # Defense-in-depth consent re-check before anything leaves the BE.
            if passes_marketing_consent_gate(candidate.profile)
            and attempt_counts.get(candidate.profile.customer_id, 0)
            < self.max_reengage_attempts_per_customer
        ]
        eligible_candidates.sort(key=lambda c: c.similarity_score, reverse=True)

        queue_entries = [
            ReengageQueueEntry(
                customer_id=candidate.profile.customer_id,
                project_key=activation.project_key,
                similarity_score=candidate.similarity_score,
                rejection_reason=candidate.profile.rejection_reason,
                budget_vnd=candidate.profile.budget_vnd,
                attempt_count=attempt_counts.get(candidate.profile.customer_id, 0) + 1,
            )
            for candidate in eligible_candidates
        ]
        if queue_entries:
            await self.reengage_queue_store.save_queue_entries(queue_entries)
        await ctx.store.set("reengage_queue_entries", queue_entries)
        return QueueUpdatedEvent()

    @step()
    async def notify_step(self, ctx: Context, ev: QueueUpdatedEvent) -> StopEvent:
        queue_entries: list[ReengageQueueEntry] = await ctx.store.get(
            "reengage_queue_entries"
        )
        return StopEvent(
            result={
                "queued_count": len(queue_entries),
                "entries": queue_entries,
                "activated_project_key": self.project_activation.project_key,
                "matched_at": datetime.now(timezone.utc).isoformat(),
            }
        )


async def run_reengage_matching_for_activated_project(
    project_activation: ProjectActivation,
    *,
    lead_repository: LeadRepository,
    need_profile_embedding: NeedProfileEmbeddingPort,
    reengage_queue_store: ReengageQueueStore,
    max_reengage_attempts_per_customer: int = MAX_REENGAGE_ATTEMPTS_PER_CUSTOMER,
) -> dict:
    """Public entry point — PublishProjectWorkflow (ISSUE-13) calls this on
    ProjectActivated; until then the admin route triggers it manually."""
    workflow = ReengageMatchWorkflow(
        project_activation=project_activation,
        lead_repository=lead_repository,
        need_profile_embedding=need_profile_embedding,
        reengage_queue_store=reengage_queue_store,
        max_reengage_attempts_per_customer=max_reengage_attempts_per_customer,
    )
    handler = workflow.run(project_activation=project_activation)
    return await handler
