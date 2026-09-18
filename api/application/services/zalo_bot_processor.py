"""Receipt-authorized Zalo execution with conservative, expiring ownership."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from api.application.ports.zalo_bot import ZALO_OUTCOME_STATUSES, ZaloAdmissionDecision, ZaloBotSender, ZaloSendError
from api.application.services.global_assistant_service import GlobalAssistantInput, GlobalAssistantService
from api.application.services.zalo_bot_identity import global_session_id, opaque_identity

logger = logging.getLogger(__name__)
_BRAKE = "Hệ thống đang nhận quá nhiều tin nhắn liên tiếp. Vui lòng chờ một lát rồi gửi lại."


@dataclass
class ZaloEvent:
    id: int
    chat_id: str
    text: str
    status: str = "pending"
    identity_key: str | None = None
    source_event_key: str | None = None
    assistant_outcome: dict | None = None
    outbound_message_id: str | None = None
    ingress_key: str | None = None
    claim_token: UUID | None = None
    attempt_count: int = 0
    lease_remaining: float = 0


class _LeaseLost(Exception):
    pass


class _CoreUncertain(Exception):
    pass


class ZaloBotProcessor:
    def __init__(self, *, store: Any, core: GlobalAssistantService,
                 sender: ZaloBotSender, identity_secret: str, settings: Any,
                 admit: Any = None) -> None:
        self._store, self._core, self._sender = store, core, sender
        self._identity_secret, self._admit, self._settings = identity_secret, admit, settings
        self._timeout = settings.zalo_bot_core_timeout_seconds
        self._margin = settings.zalo_bot_lease_margin_seconds
        self._lease = settings.zalo_bot_lease_seconds
        if self._timeout <= 0 or self._margin <= 5 or self._lease <= self._timeout + self._margin:
            raise ValueError("Invalid Zalo Bot lease configuration")
        self.halted = False
        self._processing: asyncio.Task | None = None
        self._work: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._deadline = 0.0
        self._lost: asyncio.Event | None = None

    def _enabled(self) -> bool:
        return bool(self._settings.zalo_bot_enabled) and not self.halted

    @staticmethod
    def _observe(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _cancel(self, task: asyncio.Task | None, seconds: float) -> bool:
        if task is None:
            return True
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=max(0, seconds))
        if task.done():
            self._observe(task)
            return True
        task.add_done_callback(self._observe)
        return False

    async def stop(self) -> None:
        self.halted = True
        await self._cancel(self._processing, self._margin)

    def _owned(self, minimum: float = 0) -> bool:
        return (self._lost is not None and not self._lost.is_set()
                and self._deadline - asyncio.get_running_loop().time() > minimum)

    async def _check(self, event: ZaloEvent, minimum: float = 0) -> bool:
        try:
            remaining = await self._store.check_ownership(event, minimum_remaining=minimum)
        except Exception:
            return False
        if remaining is None or remaining <= minimum:
            return False
        self._deadline = asyncio.get_running_loop().time() + remaining
        return self._owned(minimum)

    async def _renew(self, event: ZaloEvent) -> None:
        interval = min(self._lease / 3, self._margin / 2)
        renewal = None
        try:
            while self._owned():
                await asyncio.sleep(min(interval, max(0, self._deadline - asyncio.get_running_loop().time())))
                if not self._owned():
                    break
                renewal = asyncio.create_task(self._store.renew_lease(event))
                done, _ = await asyncio.wait({renewal}, timeout=min(5, max(0, self._deadline - asyncio.get_running_loop().time())))
                if not done:
                    break
                remaining = renewal.result()
                if remaining is None or remaining <= 0 or not self._owned():
                    break
                self._deadline = asyncio.get_running_loop().time() + remaining
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if renewal is not None and not renewal.done():
                renewal.cancel()
                renewal.add_done_callback(self._observe)
        self._lost.set()

    async def _execute(self, operation, request: GlobalAssistantInput):
        async def enter():
            # Recheck in the scheduled task, not before create_task's scheduling gap.
            if not self._enabled() or not self._owned(self._timeout + self._margin):
                raise _LeaseLost
            return await operation(request)

        self._work = asyncio.create_task(enter())
        loss = asyncio.create_task(self._lost.wait())
        try:
            done, _ = await asyncio.wait({self._work, loss}, timeout=self._timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if loss in done or not self._owned():
                raise _LeaseLost
            if self._work not in done:
                raise _CoreUncertain
            return self._work.result()
        finally:
            loss.cancel()
            await asyncio.gather(loss, return_exceptions=True)
            if not await self._cancel(self._work, min(self._margin, max(0, self._deadline - asyncio.get_running_loop().time()))):
                self.halted = True
                self._lost.set()
                logger.warning("Zalo Bot processing: ACTOR_LEASE_LOST")
            self._work = None

    @staticmethod
    def _valid_cache(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != {"answer", "resolution_status", "project_key", "meta"}:
            return False
        answer = value["answer"]
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
            return False
        try:
            answer.encode("utf-8")
        except UnicodeError:
            return False
        return (isinstance(value["resolution_status"], str) and value["resolution_status"] in ZALO_OUTCOME_STATUSES
                and (value["project_key"] is None or isinstance(value["project_key"], str))
                and value["meta"] == {})
    async def _access_allowed(self, event: ZaloEvent) -> bool:
        """Authorize via open-demo bypass or durable sales binding.

        Open-demo mode short-circuits before any sales/binding lookup, so
        postgres zalo_bot_bindings.authorize is never called and no sales_id
        is synthesized.
        """
        if self._settings.zalo_bot_open_demo_access:
            return True
        return bool(await self._store.authorize(event.identity_key))

    async def _failure(self, event: ZaloEvent, code: str, *, retryable: bool = True) -> str:
        logger.warning("Zalo Bot processing: %s", code)
        if not self._owned():
            return "lost_claim"
        try:
            changed = await self._store.fail(event, code, retryable=retryable)
        except Exception:
            return "retryable"
        return ("retryable" if retryable else "dead") if changed else "lost_claim"

    async def process(self, event_id: int) -> str:
        if not self._enabled() or self._processing is not None:
            return "skipped"
        self._processing = asyncio.current_task()
        event = None
        code = "STORE_UNAVAILABLE"
        try:
            event = await self._store.claim_event(event_id)
            if event is None:
                return "missing"
            self._lost = asyncio.Event()
            self._deadline = asyncio.get_running_loop().time() + event.lease_remaining
            if not self._owned():
                return "lost_claim"
            if (event.identity_key != opaque_identity(event.chat_id, self._identity_secret)
                    or event.source_event_key != f"zalo-bot:event:v1:{event.id}"):
                return await self._failure(event, "IDENTITY_INTEGRITY", retryable=False)
            self._heartbeat = asyncio.create_task(self._renew(event))
            if event.outbound_message_id is not None:
                return "done" if await self._store.complete(event) else "lost_claim"
            cached = event.assistant_outcome
            if cached is not None and not self._valid_cache(cached):
                return await self._failure(event, "CACHE_INVALID", retryable=False)
            code = "AUTH_UNAVAILABLE"
            if not await self._access_allowed(event):
                return "rejected" if self._owned() and await self._store.reject(event) else "lost_claim"
            if not self._owned():
                return "lost_claim"
            if cached is None:
                code = "ACTOR_LEASE_LOST"
                if not self._enabled() or not await self._check(event, self._timeout + self._margin):
                    await self._store.release(event)
                    return "skipped"
                code = "ADMISSION_UNAVAILABLE"
                if self._admit is None:
                    return await self._failure(event, code)
                decision = await self._admit(event.identity_key, event.source_event_key,
                                             event_id=event.id, claim_token=event.claim_token)
                if decision is ZaloAdmissionDecision.LOST_CLAIM or not self._owned():
                    return "lost_claim"
                if decision in (ZaloAdmissionDecision.BRAKED, ZaloAdmissionDecision.ALREADY_BRAKED):
                    cached = {"answer": _BRAKE, "resolution_status": "technical_brake", "project_key": None, "meta": {}}
                elif decision in (ZaloAdmissionDecision.STARTED, ZaloAdmissionDecision.ALREADY_STARTED):
                    code = "ACTOR_LEASE_LOST"
                    if not self._enabled() or not await self._check(event, self._timeout + self._margin):
                        await self._store.release(event)
                        return "skipped"
                    request = GlobalAssistantInput(query=event.text,
                        session_id=global_session_id(event.identity_key), identity_key=event.identity_key,
                        source_event_key=event.source_event_key, max_visible_chars=2000)
                    code = "CORE_OUTCOME_UNCERTAIN"
                    operation = self._core.run if decision is ZaloAdmissionDecision.STARTED else self._core.recover_source_event
                    outcome = await self._execute(operation, request)
                    if outcome is None:
                        raise _CoreUncertain
                    answer = outcome.answer
                    cached = {"answer": answer, "resolution_status": outcome.resolution_status,
                              "project_key": outcome.project_key, "meta": {}}
                    if not self._valid_cache(cached):
                        return await self._failure(event, "CACHE_INVALID", retryable=False)
                else:
                    return await self._failure(event, "ADMISSION_UNAVAILABLE")
                code = "STORE_UNAVAILABLE"
                if not self._owned() or not await self._store.cache_outcome(event, cached):
                    return "lost_claim"
            code = "AUTH_UNAVAILABLE"
            if not self._owned():
                return "lost_claim"
            if not await self._access_allowed(event):
                return "rejected" if self._owned() and await self._store.reject(event) else "lost_claim"
            if not self._enabled():
                await self._store.release(event)
                return "skipped"
            code = "ACTOR_LEASE_LOST"
            if not await self._check(event) or not self._enabled() or not self._owned():
                return "lost_claim"
            code = "SEND_UNAVAILABLE"
            message_id = await self._sender.send_text(event.chat_id, cached["answer"])
            code = "STORE_UNAVAILABLE"
            return "done" if self._owned() and await self._store.complete(event, message_id) else "lost_claim"
        except asyncio.CancelledError:
            if event is not None and self._owned() and self._work is None:
                release = asyncio.create_task(self._store.release(event))
                done, _ = await asyncio.wait({release}, timeout=min(5, self._margin))
                if not done:
                    release.cancel()
                    release.add_done_callback(self._observe)
                else:
                    self._observe(release)
            raise
        except _LeaseLost:
            logger.warning("Zalo Bot processing: ACTOR_LEASE_LOST")
            return "lost_claim"
        except ZaloSendError as exc:
            return await self._failure(event, exc.code, retryable=exc.retryable)
        except Exception:
            if event is not None:
                return await self._failure(event, code)
            logger.warning("Zalo Bot processing: %s", code)
            return "retryable"
        finally:
            await self._cancel(self._heartbeat, 0)
            self._heartbeat = None
            self._processing = None
