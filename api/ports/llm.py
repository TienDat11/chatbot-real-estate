"""Chat-completion port — contract implemented by provider adapters."""

from __future__ import annotations

from typing import AsyncIterator, Protocol, Sequence, runtime_checkable


@runtime_checkable
class LLMChatPort(Protocol):
    """Chat completions: one-shot `complete` and token `stream`."""

    async def complete(
        self,
        messages: Sequence[dict],
        *,
        json_mode: bool = False,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout: float = 30.0,
    ) -> str: ...

    async def stream(
        self,
        messages: Sequence[dict],
        *,
        model: str | None = None,
    ) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...
