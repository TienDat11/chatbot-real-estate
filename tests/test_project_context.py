"""Behavioral invariants for immutable project context snapshots."""

from __future__ import annotations

import pytest

from api.application.services.project_context import ProjectContext


@pytest.mark.parametrize("answer_mode", ["normal", "training"])
def test_context_accepts_active_project_in_each_mode(answer_mode):
    context = ProjectContext(
        project_key="camellia",
        answer_mode=answer_mode,
        active_catalogue=(("camellia", "Camellia"), ("soleil", "Soleil")),
    )

    assert context.project_key == "camellia"
    assert context.answer_mode == answer_mode
    assert context.active_catalogue == (("camellia", "Camellia"), ("soleil", "Soleil"))


def test_context_normalizes_and_freezes_catalogue_snapshot():
    catalogue = [["camellia", "Camellia"], ["soleil", "Soleil"]]
    context = ProjectContext("camellia", "normal", catalogue)

    catalogue[0][1] = "Changed"
    catalogue.append(["other", "Other"])

    assert context.active_catalogue == (("camellia", "Camellia"), ("soleil", "Soleil"))


@pytest.mark.parametrize("project_key", ["", "_legacy", "_training", "_global", "bad-key"])
def test_context_rejects_empty_reserved_or_invalid_project_key(project_key):
    with pytest.raises(ValueError):
        ProjectContext(project_key, "normal", (("camellia", "Camellia"),))


def test_context_rejects_project_not_in_active_catalogue():
    with pytest.raises(ValueError, match="not active"):
        ProjectContext("camellia", "normal", (("soleil", "Soleil"),))


def test_context_rejects_unknown_answer_mode():
    with pytest.raises(ValueError, match="answer_mode"):
        ProjectContext("camellia", "preview", (("camellia", "Camellia"),))


@pytest.mark.parametrize("catalogue", [(), (("camellia", " "),), (("camellia",),), ((None, "Camellia"),)])
def test_context_rejects_empty_or_malformed_catalogue(catalogue):
    with pytest.raises(ValueError):
        ProjectContext("camellia", "normal", catalogue)
