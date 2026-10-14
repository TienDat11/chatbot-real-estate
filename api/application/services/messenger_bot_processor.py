"""Receipt-authorized Messenger execution with conservative, expiring ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from api.application.ports.messenger_bot import MessengerAdmissionDecision, MessengerBotSender, MessengerSendError
from api.application.services.global_assistant_service import GlobalAssistantInput, GlobalAssistantService
from api.application.services.messenger_bot_identity import global_session_id, opaque_identity
from api.infrastructure.adapters.postgres_messenger_events import MessengerEvent, valid_messenger_cache
from api.application.services.messenger_bot_rendering import to_messenger_plain

logger = logging.getLogger(__name__)

_BRAKE = "Hệ thống đang nhận quá nhiều tin nhắn liên tiếp. Vui lòng chờ một lát rồi gửi lại."
_GREETING = "Xin chào! Hiện tại chúng tôi có {n} dự án. Bạn muốn tôi giới thiệu dự án nào ạ?"
_EMPTY_CATALOGUE = "Hiện chưa có dự án nào để giới thiệu."
_MENU_PAYLOAD = "SELECT_PROJECT_MENU"
_SELECT_PREFIX = "SELECT_PROJECT:"
_CHANGE_PROJECT_TITLE = "Đổi dự án"
_QUICK_REPLY_CAP = 13
_TITLE_CAP = 20
_TEXT_CAP = 2000
_INTRO_QUERY = "Giới thiệu dự án này"
_NUMBER_HINT = "Vui lòng trả lời bằng số thứ tự hoặc tên dự án."


class _LeaseLost(Exception):
    pass


class _CoreUncertain(Exception):
    pass


class MessengerBotProcessor:
    """Own one Messenger event at a time; lease loss always fails closed."""

    def __init__(self, *, store: Any, core: GlobalAssistantService,
                 sender: MessengerBotSender, identity_secret: str, settings: Any,
                 admit: Any = None,
                 catalogue_loader: Callable[[], Awaitable[list[tuple[str, str]]]],
                 media_loader: Callable[[str], Awaitable[list[str]]] | None = None) -> None:
        self._store, self._core, self._sender = store, core, sender
        self._media_loader = media_loader
        self._identity_secret, self._admit, self._settings = identity_secret, admit, settings
        self._catalogue_loader = catalogue_loader
        self._timeout = settings.messenger_core_timeout_seconds
        self._margin = settings.messenger_lease_margin_seconds
        self._lease = settings.messenger_lease_seconds
        if self._timeout <= 0 or self._margin <= 5 or self._lease <= self._timeout + self._margin:
            raise ValueError("Invalid Messenger Bot lease configuration")
        self.halted = False
        self._processing: asyncio.Task | None = None
        self._work: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._deadline = 0.0
        self._lost: asyncio.Event | None = None

    def _enabled(self) -> bool:
        return bool(self._settings.messenger_bot_enabled) and not self.halted

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

    async def _check(self, event: MessengerEvent, minimum: float = 0) -> bool:
        try:
            remaining = await self._store.check_ownership(event, minimum_remaining=minimum)
        except Exception:
            return False
        if remaining is None or remaining <= minimum:
            return False
        self._deadline = asyncio.get_running_loop().time() + remaining
        return self._owned(minimum)

    async def _renew(self, event: MessengerEvent) -> None:
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
                logger.warning("Messenger Bot processing: ACTOR_LEASE_LOST")
            self._work = None

    async def _access_allowed(self, event: MessengerEvent) -> bool:
        """Authorize via open-demo bypass or durable sales binding."""
        if self._settings.messenger_open_demo_access:
            return True
        return bool(await self._store.authorize(event.identity_key))

    async def _failure(self, event: MessengerEvent, code: str, *, retryable: bool = True) -> str:
        logger.warning("Messenger Bot processing: %s", code)
        if not self._owned():
            return "lost_claim"
        try:
            changed = await self._store.fail(event, code, retryable=retryable)
        except Exception:
            return "retryable"
        return ("retryable" if retryable else "dead") if changed else "lost_claim"

    @staticmethod
    def _numbered_chunks(catalogue: list[tuple[str, str]]) -> list[str]:
        """Split the full numbered list on line boundaries; no chunk exceeds the text cap."""
        chunks: list[str] = []
        current = _GREETING.format(n=len(catalogue))
        for i, (_, name) in enumerate(catalogue, 1):
            line = f"{i}. {name}"
            if len(line) > _TEXT_CAP:
                line = line[:_TEXT_CAP]
            piece = "\n" + line
            if len(current) + len(piece) > _TEXT_CAP:
                chunks.append(current)
                current = line
            else:
                current += piece
        if len(current) + 1 + len(_NUMBER_HINT) > _TEXT_CAP:
            chunks.append(current)
            current = _NUMBER_HINT
        else:
            current += "\n" + _NUMBER_HINT
        chunks.append(current)
        return chunks

    async def _build_picker(self, full_catalogue: list[tuple[str, str]],
                            candidate_keys: list[str] | None = None) -> dict:
        """Single picker builder for every path; overflow decided before narrowing."""
        if not full_catalogue:
            return {"reply_kind": "answer", "text": _EMPTY_CATALOGUE, "options": None,
                    "picker_keys": None, "candidate_order": None,
                    "resolution_status": "unresolved", "project_key": None}
        if len(full_catalogue) > _QUICK_REPLY_CAP:
            chunks = self._numbered_chunks(list(full_catalogue))
            return {"reply_kind": "numbered_picker", "text": chunks[0], "options": None,
                    "picker_keys": [key for key, _ in full_catalogue],
                    "candidate_order": list(candidate_keys) if candidate_keys else None,
                    "resolution_status": "unresolved", "project_key": None}
        wanted = set(candidate_keys) if candidate_keys else None
        entries = [(key, name) for key, name in full_catalogue if key in wanted] if wanted else list(full_catalogue)
        if not entries:
            entries = list(full_catalogue)
        reply: dict = {"reply_kind": "quick_replies", "text": _GREETING.format(n=len(full_catalogue)),
                       "options": [[name[:_TITLE_CAP], f"{_SELECT_PREFIX}{key}"] for key, name in entries],
                       "picker_keys": None,
                       "candidate_order": list(candidate_keys) if candidate_keys else None,
                       "resolution_status": "unresolved", "project_key": None}
        # Media must not precede the customer's choice: the picker's project_key
        # is None, so attaching one project's floor plans would advertise it.
        return reply

    async def _run_intro(self, event: MessengerEvent, key: str) -> dict:
        """Scoped intro for a confirmed project; never resumes a pending intent."""
        # The numeric and name paths resolve `key` from a picker snapshot or a
        # name match, and neither survives a replay: a later picker can
        # overwrite the snapshot and the catalogue can change. Persisting the
        # resolved key before core runs is what makes the replay re-derive the
        # same project. A lost lease must not run core on an unpersisted key.
        if not await self._store.record_selection(event, key):
            raise _LeaseLost
        request = GlobalAssistantInput(query=_INTRO_QUERY,
            session_id=global_session_id(event.identity_key), identity_key=event.identity_key,
            selected_project_key=key, source_event_key=event.source_event_key,
            max_visible_chars=2000, resume_pending_intent=False)
        outcome = await self._execute(self._core.run, request)
        if outcome is None:
            raise _CoreUncertain
        reply: dict = {"reply_kind": "quick_replies", "text": outcome.answer,
                       "options": [[_CHANGE_PROJECT_TITLE, _MENU_PAYLOAD]],
                       "picker_keys": None, "candidate_order": None,
                       "resolution_status": outcome.resolution_status,
                       "project_key": outcome.project_key if outcome.project_key is not None else key}
        if self._media_loader is not None and reply["project_key"]:
            try:
                urls = await self._media_loader(reply["project_key"])
            except Exception:
                urls = []
            if urls:
                reply["images"] = urls[:3]
        return reply

    async def _resolve_selection(self, event: MessengerEvent, key: str | None) -> dict:
        """Intro for an active project, otherwise a refreshed picker without RAG."""
        if key is None:
            return await self._build_picker(await self._catalogue_loader())
        catalogue = await self._catalogue_loader()
        if key not in {known for known, _ in catalogue}:
            return await self._build_picker(catalogue)
        return await self._run_intro(event, key)

    async def _dispatch_message(self, event: MessengerEvent) -> dict:
        """Numeric reply resolves via the stored picker snapshot; a lone name matches one project."""
        if event.selected_project_key is not None:
            # A previous attempt already resolved and persisted this event's
            # project, so honour it instead of resolving again. Re-resolving
            # would read the picker snapshot and the catalogue as they are now,
            # and either can have moved on since the first attempt: a later
            # picker rewrites the snapshot, and the catalogue changes. Only
            # select_project events carry a key from the projection, and those
            # never reach here, so this branch means a replay of a message.
            return await self._resolve_selection(event, event.selected_project_key)
        try:
            n = int((event.text or "").strip())
        except (TypeError, ValueError):
            n = None
        if n is not None:
            snapshot = await self._store.latest_picker_snapshot(event.identity_key)
            if snapshot is not None and 1 <= n <= len(snapshot):
                return await self._resolve_selection(event, snapshot[n - 1])
        catalogue = await self._catalogue_loader()
        folded = (event.text or "").strip().casefold()
        hits = [key for key, name in catalogue
                if folded and (folded == name.strip().casefold() or folded == key.casefold())]
        if len(hits) == 1:
            return await self._resolve_selection(event, hits[0])
        request = GlobalAssistantInput(query=event.text,
            session_id=global_session_id(event.identity_key), identity_key=event.identity_key,
            source_event_key=event.source_event_key, max_visible_chars=2000)
        outcome = await self._execute(self._core.run, request)
        if outcome is None:
            raise _CoreUncertain
        if outcome.resolution_status in ("resolved", "replayed"):
            reply: dict = {"reply_kind": "quick_replies", "text": outcome.answer,
                           "options": [[_CHANGE_PROJECT_TITLE, _MENU_PAYLOAD]],
                           "picker_keys": None, "candidate_order": None,
                           "resolution_status": outcome.resolution_status,
                           "project_key": outcome.project_key}
            if self._media_loader is not None and reply["project_key"]:
                try:
                    urls = await self._media_loader(reply["project_key"])
                except Exception:
                    urls = []
                if urls:
                    reply["images"] = urls[:3]
            return reply
        return await self._build_picker(
            catalogue, candidate_keys=list(outcome.candidates) if outcome.candidates else None)

    async def _dispatch_kind(self, event: MessengerEvent) -> dict:
        """Build the cache envelope; every branch returns the shared cache shape."""
        if event.kind == "get_started":
            catalogue = await self._catalogue_loader()
            if not catalogue:
                return {"reply_kind": "answer", "text": _EMPTY_CATALOGUE, "options": None,
                        "picker_keys": None, "candidate_order": None,
                        "resolution_status": "unresolved", "project_key": None}
            return await self._build_picker(catalogue)
        if event.kind == "select_project":
            return await self._resolve_selection(event, event.selected_project_key)
        return await self._dispatch_message(event)

    async def _deliver(self, event: MessengerEvent, cached: dict) -> list[str]:
        """Send the cached envelope in order; render text to plain at delivery
        time so the stored envelope stays raw, then attach best-effort images.
        """
        kind = cached["reply_kind"]

        def _render(text: str) -> str:
            rendered = to_messenger_plain(text)
            return rendered if rendered.strip() else text

        if kind in ("answer", "brake"):
            message_ids = [await self._sender.send_text(event.psid, _render(cached["text"]))]
        elif kind == "quick_replies":
            message_ids = [await self._sender.send_quick_replies(
                event.psid, _render(cached["text"]),
                [(title, payload) for title, payload in cached["options"]])]
        else:
            catalogue = await self._catalogue_loader()
            names = {key: name for key, name in catalogue}
            ordered = [(key, names.get(key, key)) for key in cached["picker_keys"] or []]
            rest = self._numbered_chunks(ordered)[1:] if ordered else []
            message_ids = [await self._sender.send_text(event.psid, _render(cached["text"]))]
            for chunk in rest:
                message_ids.append(await self._sender.send_text(event.psid, _render(chunk)))

        # Text went out already; a failing image send must not undo it, and
        # raising here would resend the text on retry. Log instead, so a
        # silently missing image (rate limit, dead URL) stays diagnosable.
        images = cached.get("images")
        if images:
            for url in images:
                try:
                    message_ids.append(await self._sender.send_image(event.psid, url))
                except MessengerSendError as exc:
                    logger.warning(
                        "messenger image send failed: code=%s retryable=%s",
                        exc.code,
                        exc.retryable,
                    )
        return message_ids

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
            if (event.identity_key != opaque_identity(event.psid, self._identity_secret)
                    or event.source_event_key != f"messenger-bot:event:v1:{event.id}"):
                return await self._failure(event, "IDENTITY_INTEGRITY", retryable=False)
            self._heartbeat = asyncio.create_task(self._renew(event))
            if event.outbound_message_ids is not None:
                return "done" if await self._store.complete(event, list(event.outbound_message_ids)) else "lost_claim"
            cached = event.assistant_outcome
            if cached is not None and not valid_messenger_cache(cached):
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
                if decision is MessengerAdmissionDecision.LOST_CLAIM or not self._owned():
                    return "lost_claim"
                if decision in (MessengerAdmissionDecision.BRAKED, MessengerAdmissionDecision.ALREADY_BRAKED):
                    cached = {"reply_kind": "brake", "text": _BRAKE, "options": None,
                              "picker_keys": None, "candidate_order": None,
                              "resolution_status": "technical_brake", "project_key": None}
                elif decision in (MessengerAdmissionDecision.STARTED, MessengerAdmissionDecision.ALREADY_STARTED):
                    code = "ACTOR_LEASE_LOST"
                    if not self._enabled() or not await self._check(event, self._timeout + self._margin):
                        await self._store.release(event)
                        return "skipped"
                    code = "CORE_OUTCOME_UNCERTAIN"
                    cached = await self._dispatch_kind(event)
                    if not valid_messenger_cache(cached):
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
            message_ids = await self._deliver(event, cached)
            code = "STORE_UNAVAILABLE"
            return "done" if self._owned() and await self._store.complete(event, message_ids) else "lost_claim"
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
            logger.warning("Messenger Bot processing: ACTOR_LEASE_LOST")
            return "lost_claim"
        except MessengerSendError as exc:
            return await self._failure(event, exc.code, retryable=exc.retryable)
        except Exception:
            if event is not None:
                return await self._failure(event, code)
            logger.warning("Messenger Bot processing: %s", code)
            return "retryable"
        finally:
            await self._cancel(self._heartbeat, 0)
            self._heartbeat = None
            self._processing = None
