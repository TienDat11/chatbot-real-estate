"""RAG pipeline prewarm (startup background task) tests.

The prewarm reuses the exact first-query init path (rag_leg._get_rag:
LightRAG singleton + one-shot initialize_storages) so the ~30-40s cold init
happens while the worker boots. These tests pin the contract: flag gating,
success completing the init, and failure degrading to the lazy path — log,
retry ONCE, then give up, never crashing startup.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

import api.interfaces.api.main as main_module
from api.interfaces.api.main import (
    RAG_PREWARM_TIMEOUT_S,
    _maybe_start_rag_prewarm,
    _prewarm_rag_pipeline,
)

# The prewarm imports _get_rag lazily inside the function; patching the
# rag_leg module attribute swaps the seam for every call site.
_RAG_LEG_GET_RAG = "api.application.services.rag_leg._get_rag"


def _config(**overrides) -> dict:
    config = {"rag_prewarm_enabled": True}
    config.update(overrides)
    return config


def test_prewarm_timeout_budget_is_generous() -> None:
    # The bound must cover the measured ~30-40s cold init (with headroom) or a
    # legitimate warm-up would be cut and the lazy path would pay the cost.
    assert RAG_PREWARM_TIMEOUT_S >= 60.0


def test_prewarm_task_not_started_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module,
        "get_cfg",
        lambda key, default=None: _config(rag_prewarm_enabled=False).get(key, default),
    )
    assert _maybe_start_rag_prewarm() is None


@pytest.mark.asyncio
async def test_prewarm_task_starts_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module, "get_cfg", lambda key, default=None: _config().get(key, default)
    )
    monkeypatch.setattr(_RAG_LEG_GET_RAG, AsyncMock(return_value=object()))
    task = _maybe_start_rag_prewarm()
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_prewarm_success_initializes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    init = AsyncMock(return_value=object())
    monkeypatch.setattr(_RAG_LEG_GET_RAG, init)
    await _prewarm_rag_pipeline()
    # One clean attempt, no retry on the happy path.
    init.assert_awaited_once()


@pytest.mark.asyncio
async def test_prewarm_failure_retries_once_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # A persistent failure must NOT crash or loop forever: two attempts (the
    # initial one + one retry) then give up, leaving the lazy path intact.
    init = AsyncMock(side_effect=RuntimeError("embedding gateway down"))
    monkeypatch.setattr(_RAG_LEG_GET_RAG, init)
    await _prewarm_rag_pipeline()
    assert init.await_count == 2


@pytest.mark.asyncio
async def test_prewarm_succeeds_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Transient failure: the retry attempt warms the pipeline; no give-up.
    init = AsyncMock(side_effect=[RuntimeError("transient"), object()])
    monkeypatch.setattr(_RAG_LEG_GET_RAG, init)
    await _prewarm_rag_pipeline()
    assert init.await_count == 2
