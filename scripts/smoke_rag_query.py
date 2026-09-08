"""Live smoke probe for the re-embedded Camellia corpus (Wave B P0 evidence).

WHY: after migrating lightrag_vdb_* to the gemini-embedding-001 space we must
prove the query path (rag_leg -> LightRAG aquery, mode from settings) retrieves
sources and concrete unit-type figures again. Read-only; prints a bounded
answer excerpt plus source count. Credentials come from .env only.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> int:
    from datetime import date

    from api.application.services.rag_leg import run_rag_leg
    from api.infrastructure.config.config import get_settings

    settings = get_settings()
    result = await run_rag_leg(
        "Các loại căn hộ tại Camellia có những loại nào, mỗi loại diện tích bao nhiêu?",
        hl=[],
        ll=[],
        as_of=date.today(),
        project_key="camellia",
    )
    answer = getattr(result, "answer", "") or ""
    sources = getattr(result, "sources", None) or []
    print(
        f"mode={settings.rag_query_mode} sources={len(sources)} answer_chars={len(answer)} "
        f"degraded={getattr(result, 'degraded', None)} error={getattr(result, 'error', None)} "
        f"reasons={getattr(result, 'degraded_reasons', None)}"
    )
    chunks = getattr(result, "chunks", None) or []
    print(f"chunks={len(chunks)}")
    for c in chunks[:3]:
        print(f"  chunk score={c.get('score')} doc_id={c.get('doc_id')} content={str(c.get('content'))[:120]}")
    for i, s in enumerate(sources[:5]):
        label = getattr(s, "label", None) or getattr(s, "title", None) or str(s)[:80]
        print(f"  source[{i}]: {label}")
    print("--- answer excerpt ---")
    print(answer[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
