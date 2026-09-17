"""Authorized Zalo transport glue around the shared Global Assistant core."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from api.application.ports.zalo_bot import ZaloBotSender
from api.application.services.global_assistant_service import (
    GlobalAssistantInput,
    GlobalAssistantService,
)
from api.application.services.zalo_bot_identity import global_session_id, opaque_identity

logger = logging.getLogger(__name__)
_TRUNCATION_SUFFIX = "\n[Đã rút gọn]"


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


class ZaloBotProcessor:
    def __init__(
        self,
        *,
        store: Any,
        core: GlobalAssistantService,
        sender: ZaloBotSender,
        identity_secret: str,
        admit: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._core = core
        self._sender = sender
        self._identity_secret = identity_secret
        self._admit = admit

    async def process(self, event_id: int) -> str:
        """Return a safe transport result; infrastructure failures remain pending.

        No concurrency claim or technical brake is implemented here (ZB-03).
        Without injected admission, uncached processing stays disabled.
        """
        try:
            event = await self._store.get_event(event_id)
            if event is None:
                return "missing"
            identity = opaque_identity(event.chat_id, self._identity_secret)
            source = f"zalo-bot:event:v1:{event.id}"
            if await self._store.authorize(identity) is None:
                await self._store.set_status(event.id, "rejected")
                return "rejected"
            if event.status == "rejected":
                return "rejected"
            if event.outbound_message_id is not None:
                await self._store.set_status(event.id, "done")
                return "done"
            if event.status == "done":
                return "done"

            cached = event.assistant_outcome
            if cached is None:
                if self._admit is None or not await self._admit(identity, source):
                    return "blocked"
                await self._store.set_identity(event.id, identity, source)
                outcome = await self._core.run(
                    GlobalAssistantInput(
                        query=event.text,
                        session_id=global_session_id(identity),
                        identity_key=identity,
                        source_event_key=source,
                    )
                )
                answer = outcome.answer
                if len(answer) > 2000:
                    answer = answer[: 2000 - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
                cached = {
                    "answer": answer,
                    "resolution_status": outcome.resolution_status,
                    "project_key": outcome.project_key,
                    "meta": {},
                }
                await self._store.cache_outcome(event.id, cached)

            # A binding or sales row may have been revoked while core/cache ran.
            if await self._store.authorize(identity) is None:
                await self._store.set_status(event.id, "rejected")
                return "rejected"
            message_id = await self._sender.send_text(event.chat_id, cached["answer"])
            # Provider acceptance before this commit remains a duplicate-send window.
            await self._store.record_outbound(event.id, message_id)
            await self._store.set_status(event.id, "done")
            return "done"
        except Exception as exc:
            # Never include exception text, traceback, chat identity, or provider URL.
            logger.warning("Zalo Bot processing failed (%s)", type(exc).__name__)
            return "retryable"
