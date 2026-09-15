"""Eval wiring contract — eval/run_eval.py resolves the canonical pipeline.

The runner used to import `api.workflow` — a path kept alive only by a
`sys.modules` alias in api/__init__.py — behind a bare `except Exception` that
could downgrade a broken install to `RagQueryPipeline = None`. These tests lock
the replacement: the canonical class, a settings-free constructor, and a loud
RuntimeError when the import path breaks — never a silent `None`.

No network, no PostgreSQL, no LLM: the facade's __init__ only stores `on_event`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from api.application.pipelines.workflow import RagQueryPipeline as CanonicalPipeline
from eval import run_eval

_RUN_EVAL_PATH = Path(run_eval.__file__).resolve()


def test_module_resolves_canonical_pipeline_class() -> None:
    resolved = run_eval.RagQueryPipeline
    assert resolved is not None, "eval must never keep a None sentinel pipeline"
    assert resolved is CanonicalPipeline
    assert resolved.__module__ == "api.application.pipelines.workflow"


def test_build_pipeline_returns_canonical_instance() -> None:
    pipeline = run_eval._build_pipeline(run_eval.EvalSettings())
    assert isinstance(pipeline, CanonicalPipeline)


def test_broken_canonical_import_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None sys.modules entry makes the import raise ImportError without
    # touching the real cached module. Exec a throwaway copy under a unique
    # name so the shared run_eval module stays pristine either way.
    monkeypatch.setitem(sys.modules, "api.application.pipelines.workflow", None)
    spec = importlib.util.spec_from_file_location("_run_eval_import_probe", _RUN_EVAL_PATH)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations via sys.modules[cls.__module__].
    monkeypatch.setitem(sys.modules, spec.name, probe)
    with pytest.raises(RuntimeError) as excinfo:
        spec.loader.exec_module(probe)
    assert "api.application.pipelines.workflow" in str(excinfo.value)
    cause = excinfo.value.__cause__
    assert isinstance(cause, ImportError), "original ImportError must be chained"
    assert not hasattr(probe, "RagQueryPipeline"), "failure must raise before any binding, never assign None"
