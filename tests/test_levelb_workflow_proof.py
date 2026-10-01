"""Level B — the production-shaped proof over a REAL disposable registry.

``tests/test_final_answer_integrity.py`` proves the byte invariant with a
hand-built pool and a list-appending audit. This module proves the same
PUBLICATION chain against a real PostgreSQL database, so every claim below is
decided by production code reading production SQL:

    real ``rag_leg._post_filter``  (registry JOIN, project + validity filter)
    -> real ``merge.hydrate_chunks`` (second JOIN, exact-content check)
    -> real ``generate.build_messages`` (serialized RAG_CONTEXT)
    -> real ``guard_output`` + the real publication gate
    -> real ``audit.write_audit`` -> a ``query_audit`` row

Only the LightRAG seam and the LLM stream are fake; the SQL leg returns zero
facts, so any published claim must rest on a seeded registry row.

    LEVELB_TEST_PG_ADMIN=postgresql://ragre:pw@127.0.0.1:5432/postgres \
        python -m pytest -q tests/test_levelb_workflow_proof.py
"""

from __future__ import annotations

import json

import api.application.pipelines.workflow as workflow_module
from api.application.pipelines.workflow import (
    LOW_GROUNDING_MESSAGE,
    VERIFICATION_FALLBACK_MESSAGE,
)
from tests.levelb_registry_harness import (
    LevelBHarness,
    camellia_context,
    levelb_registry,  # noqa: F401 — fixture
    opened_window,
    registry,
    seed_registry,
    sha256_hex,
    soleil_context,
)

SOLEIL_DOC = "levelb-soleil-profile"
SOLEIL_CHUNK = "levelb-soleil-profile:1:0"
CAMELLIA_DOC = "levelb-camellia-profile"
CAMELLIA_CHUNK = "levelb-camellia-profile:1:0"
# S5 seeds chunk_id == doc_id on purpose: with one chunk per document the
# selected-prompt ids and the emitted doc ids must be the SAME set, which is
# the invariant the plan asks that scenario to pin.
LOW_DOC = "levelb-low-evidence"
LOW_CHUNK = "levelb-low-evidence"

SOLEIL_CONTENT = "Soleil nằm ven biển với hồ bơi chung và vườn cảnh trên cao."
CAMELLIA_CONTENT = "Camellia có vườn cây xanh và lối dạo bộ cho cư dân."
LOW_CONTENT = "Soleil có bãi đỗ xe rộng và khu thể thao ngoài trời."

SOLEIL_DRAFT = "Soleil có hồ bơi chung và vườn cảnh trên cao theo hồ sơ dự án."
CAMELLIA_DRAFT = "Camellia có vườn cây xanh và lối dạo bộ cho cư dân theo hồ sơ dự án."
LOW_DRAFT = "Soleil có bãi đỗ xe rộng và khu thể thao ngoài trời theo hồ sơ dự án."


def test_s2_soleil_descriptive_rag_only_publishes_from_real_registry(
    monkeypatch, levelb_registry  # noqa: F811
):
    """S2 — a Soleil descriptive turn publishes on registry evidence alone.

    The registry row is real, the post-filter that kept it is real, the
    hydration that produced the prompt is real, and the row this asserts on is
    the row the REAL ``write_audit`` inserted.
    """
    start, _end = opened_window(levelb_registry["today"])
    with registry(levelb_registry) as cur:
        seed_registry(
            cur, doc_id=SOLEIL_DOC, chunk_id=SOLEIL_CHUNK, content=SOLEIL_CONTENT,
            project_key="soleil", title="Hồ sơ tiện ích Soleil",
            effective_from=start,
        )

    harness = LevelBHarness(
        monkeypatch, db=levelb_registry, project=soleil_context(),
        query="Soleil có tiện ích gì ạ?", draft=SOLEIL_DRAFT,
        rag_chunks={SOLEIL_CHUNK: SOLEIL_CONTENT}, session_id="levelb-s2",
    )
    result = harness.run()

    # REAL answer: the substantive draft, not a canned safe copy.
    assert result["answer"] == SOLEIL_DRAFT
    assert result["answer"] != VERIFICATION_FALLBACK_MESSAGE
    assert result["answer"] != LOW_GROUNDING_MESSAGE
    assert result["confidence"] in {"MEDIUM", "HIGH"}

    # REAL registry read: only the Soleil document was emitted.
    assert [s["doc_id"] for s in result["sources"]] == [SOLEIL_DOC]

    # REAL audit row: the ids build_messages really serialized.
    row = harness.assert_bytes_agree(result, SOLEIL_DRAFT)
    selected = row["guard_verdicts"]["selected_prompt_chunk_ids"]
    assert selected == [SOLEIL_CHUNK]

    # ZERO Camellia evidence anywhere: not in the sources, not in the prompt
    # the gate was decided against, not in the persisted audit row.
    assert CAMELLIA_DOC not in [s["doc_id"] for s in result["sources"]]
    assert CAMELLIA_CHUNK not in harness.prompt()
    assert "camellia" not in harness.prompt().lower()
    assert CAMELLIA_CHUNK not in row["chunk_ids"]
    assert "camellia" not in json.dumps(row["guard_verdicts"]).lower()

    # Byte identity: token stream, returned answer, real audit hash.
    assert harness.tokens() == SOLEIL_DRAFT
    assert row["answer_hash"] == sha256_hex(result["answer"])


def test_s3_foreign_chunk_cannot_gain_publication_authority(monkeypatch, levelb_registry):  # noqa: F811
    """S3 — LightRAG surfaces an in-scope AND a foreign chunk; only one ships.

    The real gate takes the PUBLISH branch here, not the fail-closed branch:
    ``rag_leg._post_filter`` filters the foreign row out on
    ``documents.project_key`` before ``merge_context`` ever hydrates it, so the
    turn stays publishable with the Camellia document alone and the foreign id
    reaches neither the prompt, the sources, nor the audit row.
    """
    start, _end = opened_window(levelb_registry["today"])
    with registry(levelb_registry) as cur:
        seed_registry(
            cur, doc_id=CAMELLIA_DOC, chunk_id=CAMELLIA_CHUNK, content=CAMELLIA_CONTENT,
            project_key="camellia", title="Hồ sơ tiện ích Camellia",
            effective_from=start,
        )
        seed_registry(
            cur, doc_id=SOLEIL_DOC, chunk_id=SOLEIL_CHUNK, content=SOLEIL_CONTENT,
            project_key="soleil", title="Hồ sơ tiện ích Soleil",
            effective_from=start,
        )

    harness = LevelBHarness(
        monkeypatch, db=levelb_registry, project=camellia_context(),
        query="Camellia có tiện ích gì ạ?", draft=CAMELLIA_DRAFT,
        rag_chunks={CAMELLIA_CHUNK: CAMELLIA_CONTENT, SOLEIL_CHUNK: SOLEIL_CONTENT},
        session_id="levelb-s3",
    )
    result = harness.run()

    # The fake really did hand the pipeline foreign evidence (not vacuous).
    assert set(harness.rag_chunk_ids) == {CAMELLIA_CHUNK, SOLEIL_CHUNK}

    # Branch A taken by the REAL gate: published on in-scope evidence only.
    assert result["answer"] == CAMELLIA_DRAFT
    assert result["answer"] != VERIFICATION_FALLBACK_MESSAGE
    assert [s["doc_id"] for s in result["sources"]] == [CAMELLIA_DOC]

    row = harness.assert_bytes_agree(result, CAMELLIA_DRAFT)
    selected = row["guard_verdicts"]["selected_prompt_chunk_ids"]
    assert selected == [CAMELLIA_CHUNK]

    # The foreign row gained no authority at any hop.
    assert SOLEIL_DOC not in [s["doc_id"] for s in result["sources"]]
    assert SOLEIL_CHUNK not in selected
    assert SOLEIL_CHUNK not in row["chunk_ids"]
    assert SOLEIL_CHUNK not in harness.prompt()
    assert "soleil" not in json.dumps(row["guard_verdicts"]).lower()


def test_s5_low_verdict_over_valid_evidence_still_publishes(monkeypatch, levelb_registry):  # noqa: F811
    """S5 — a COMPLETED LOW/review verdict over valid current evidence ships.

    ``guard_output`` is forced to the completed LOW verdict (no outage, no
    exception). Hydration and the audit stay real: the evidence behind the
    review flag is a real, current, project-scoped registry row.
    """
    start, _end = opened_window(levelb_registry["today"])
    with registry(levelb_registry) as cur:
        seed_registry(
            cur, doc_id=LOW_DOC, chunk_id=LOW_CHUNK, content=LOW_CONTENT,
            project_key="soleil", title="Hồ sơ tiện ích Soleil",
            effective_from=start,
        )

    low_verdict = workflow_module.OutputGuardResult(
        confidence="LOW", requires_review=True, verdicts={}
    )
    harness = LevelBHarness(
        monkeypatch, db=levelb_registry, project=soleil_context(),
        query="Soleil có bãi đỗ xe không ạ?", draft=LOW_DRAFT,
        rag_chunks={LOW_CHUNK: LOW_CONTENT}, session_id="levelb-s5",
        guard_result=low_verdict,
    )
    result = harness.run()

    # A reviewed answer, not the fallback: the LOW tier alone never blocks.
    assert result["answer"] == LOW_DRAFT
    assert result["answer"] != VERIFICATION_FALLBACK_MESSAGE
    assert result["answer"] != LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True

    row = harness.assert_bytes_agree(result, LOW_DRAFT)
    selected = row["guard_verdicts"]["selected_prompt_chunk_ids"]
    emitted_doc_ids = [s["doc_id"] for s in result["sources"]]
    assert emitted_doc_ids == [LOW_DOC]
    assert selected == emitted_doc_ids
    assert row["answer_hash"] == sha256_hex(result["answer"])