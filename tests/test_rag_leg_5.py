import asyncio

import pytest


@pytest.mark.asyncio
async def test_conv_handler_cancellation_is_awaited(monkeypatch):
    from api.application.pipelines.conv_workflow import RagRgreConvWorkflow

    class CancellableHandler:
        # Model a real handler: once cancel() is called, any later await
        # settles immediately with CancelledError instead of blocking.
        def __init__(self):
            self.cancelled = False
            self.settled = False

        def cancel(self):
            self.cancelled = True

        def __await__(self):
            return self._wait().__await__()

        async def _wait(self):
            if self.cancelled:
                self.settled = True
                raise asyncio.CancelledError()
            await asyncio.Event().wait()

    handler = CancellableHandler()
    workflow = RagRgreConvWorkflow.__new__(RagRgreConvWorkflow)
    workflow._inner = type("Inner", (), {"run": lambda *_args, **_kwargs: handler})()
    workflow._flush_stream_tail = lambda: asyncio.sleep(0)
    task = asyncio.create_task(workflow.run_inner(None, type("Ev", (), {
        "conv": {}, "query": "q", "session_id": None, "as_of": None,
        "history": [], "project_key": None, "device_id": None,
    })()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handler.cancelled is True
    assert handler.settled is True


def test_project_workspace_routes_soleil_only():
    """Soleil reads its dedicated workspace (2026-08-28 scope-repair migration);
    every other project keeps the historical default-workspace instance."""
    from api.application.services import rag_leg
    from api.infrastructure.config.config import get_settings

    assert rag_leg._project_workspace("soleil") == get_settings().lightrag_workspace_soleil
    assert rag_leg._project_workspace("camellia") is None
    assert rag_leg._project_workspace(None) is None
    assert rag_leg._project_workspace("unknown-project") is None
