import importlib

from api.application.services import rag_leg


def test_aquery_timeout_env_override(monkeypatch):
    monkeypatch.setenv("RAG_AQUERY_TIMEOUT_S", "7.5")
    reloaded = importlib.reload(rag_leg)
    try:
        assert reloaded.AQUERY_TIMEOUT_S == 7.5
    finally:
        monkeypatch.delenv("RAG_AQUERY_TIMEOUT_S", raising=False)
        importlib.reload(rag_leg)
