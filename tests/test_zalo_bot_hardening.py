"""Local failure injection; not proof of deployed cancellation or DB clock safety."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_zalo_bot_processor import (
    ANSWER, SECRET, forbidden_business_effects, rig,
)
from api.application.services.zalo_bot_processor import ZaloBotProcessor


def test_delayed_entry_preserves_receipt_without_running(rig):
    async def scenario():
        original = rig.store.check_ownership
        count = 0

        async def check(event, *, minimum_remaining=0):
            nonlocal count
            count += 1
            if count == 2:
                return minimum_remaining
            return await original(event, minimum_remaining=minimum_remaining)

        rig.store.check_ownership = check
        assert await rig.processor.process(1) == "skipped"
        rig.core.run.assert_not_called()
        rig.store.check_ownership = original
        assert await rig.processor.process(1) == "retryable"
        rig.core.run.assert_not_called()
        rig.core.recover_source_event.assert_awaited_once()
        assert rig.store.codes[-1] == "CORE_OUTCOME_UNCERTAIN"

    asyncio.run(scenario())


def test_stale_cache_result_cannot_send_or_finalize(rig):
    async def scenario():
        original = rig.store.cache_outcome

        async def stale(event, outcome):
            rig.store.events[event.id].claim_token = None
            return await original(event, outcome)

        rig.store.cache_outcome = stale
        assert await rig.processor.process(1) == "lost_claim"
        assert rig.store.events[1].assistant_outcome is None
        rig.sender.send_text.assert_not_called()
        assert rig.store.events[1].outbound_message_id is None

    asyncio.run(scenario())


def test_timeout_cancels_cooperative_core_then_replay_only(rig):
    async def scenario():
        stopped = asyncio.Event()

        async def core(request):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        rig.core.run.side_effect = core
        rig.processor._timeout = .02
        assert await rig.processor.process(1) == "retryable"
        assert stopped.is_set()
        assert not rig.processor.halted
        assert await rig.processor.process(1) == "retryable"
        assert rig.core.run.await_count == 1
        rig.core.recover_source_event.assert_awaited_once()
        rig.sender.send_text.assert_not_called()

    asyncio.run(scenario())


def test_noncooperative_timeout_halts_without_early_release(rig):
    async def scenario():
        finish = asyncio.Event()
        stopped = asyncio.Event()

        async def core(request):
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
            finally:
                stopped.set()
            return SimpleNamespace(answer=ANSWER, resolution_status="resolved", project_key="camellia")

        rig.core.run.side_effect = core
        rig.processor._timeout = .01
        rig.processor._margin = .02
        try:
            assert await rig.processor.process(1) == "lost_claim"
            assert rig.processor.halted
            assert not stopped.is_set()
            assert rig.store.events[1].status == "processing"
            assert rig.store.codes == []
            assert await rig.processor.process(2) == "skipped"
        finally:
            finish.set()
            await asyncio.wait_for(stopped.wait(), 1)
        rig.sender.send_text.assert_not_called()
        assert rig.store.events[1].assistant_outcome is None

    asyncio.run(scenario())


def test_shutdown_is_bounded_and_stops_new_claims(rig):
    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def core(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        rig.core.run.side_effect = core
        task = asyncio.create_task(rig.processor.process(1))
        await started.wait()
        await rig.processor.stop()
        assert stopped.is_set()
        assert task.cancelled()
        assert rig.processor.halted
        assert await rig.processor.process(1) == "skipped"
        assert rig.store.events[1].status == "failed"
        rig.sender.send_text.assert_not_called()

    asyncio.run(scenario())


def test_stalled_renewal_does_not_extend_local_expiry(rig):
    async def scenario():
        stopped = asyncio.Event()

        async def core(request):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        renewals = 0

        async def renew(event):
            nonlocal renewals
            renewals += 1
            if renewals == 1:
                return .02
            await asyncio.Event().wait()

        rig.core.run.side_effect = core
        rig.store.renew_lease = renew
        rig.processor._timeout = .2
        rig.processor._margin = .01
        assert await rig.processor.process(1) == "lost_claim"
        assert renewals == 2
        assert stopped.is_set()
        rig.sender.send_text.assert_not_called()

    asyncio.run(scenario())


def test_renewal_loss_cancels_core_without_transport_writes(rig):
    async def scenario():
        stopped = asyncio.Event()

        async def core(request):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        rig.core.run.side_effect = core
        rig.store.renew_lease = AsyncMock(return_value=None)
        rig.processor._margin = .02
        assert await rig.processor.process(1) == "lost_claim"
        assert stopped.is_set()
        assert rig.store.codes == []
        assert rig.store.writes == []
        rig.sender.send_text.assert_not_called()

    asyncio.run(scenario())


def test_forward_db_clock_jump_allows_different_source_overlap_but_not_same_source(rig):
    async def scenario():
        # Fake DB clock is independent of the event loop's monotonic clock.
        clock = [0.0]
        actor = {"token": None, "until": 0.0}
        store = rig.store
        claim = store.claim_event

        async def clock_claim(event_id):
            if actor["until"] > clock[0]:
                return None
            event = store.events.get(event_id)
            if event is not None and event.status == "processing":
                event.status = "failed"
            result = await claim(event_id)
            if result:
                actor.update(token=result.claim_token, until=clock[0] + 120)
            return result

        old_owns = store.owns
        store.claim_event = clock_claim
        store.owns = lambda event: (old_owns(event) and actor["token"] == event.claim_token
                                    and actor["until"] > clock[0])
        store.events[2] = store.event(2, "other source")
        active = set()
        overlap = asyncio.Event()
        entered = asyncio.Event()
        finish = asyncio.Event()
        stopped = asyncio.Event()

        async def cooperative_core(request):
            active.add(request.source_event_key)
            entered.set()
            if len(active) == 2:
                overlap.set()
            try:
                await finish.wait()
            finally:
                active.discard(request.source_event_key)
                stopped.set()
            return SimpleNamespace(answer=ANSWER, resolution_status="resolved", project_key="camellia")

        rig.core.run.side_effect = cooperative_core
        second = ZaloBotProcessor(store=store, core=rig.core, sender=rig.sender,
                                  identity_secret=SECRET, settings=rig.settings, admit=rig.admission)
        first_task = asyncio.create_task(rig.processor.process(1))
        await entered.wait()
        assert await second.process(2) == "missing"
        clock[0] = 121  # Forward DB jump invalidates lease before old heartbeat sees it.
        second_task = asyncio.create_task(second.process(2))
        await asyncio.wait_for(overlap.wait(), 1)
        assert len(active) == 2  # Explicit F7, even with cooperative core.
        await rig.processor.stop()
        assert first_task.cancelled()
        assert "zalo-bot:event:v1:1" not in active
        finish.set()
        assert await second_task == "done"
        clock[0] = 242
        # Same source reclaims but immutable receipt permits replay read only.
        assert await second.process(1) == "retryable"
        assert [call.args[0].source_event_key for call in rig.core.run.await_args_list] == [
            "zalo-bot:event:v1:1", "zalo-bot:event:v1:2",
        ]
        rig.core.recover_source_event.assert_awaited_once()

    asyncio.run(scenario())
