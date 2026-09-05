"""Full-pipeline smoke: real answer generation through RagQueryPipelineConv.

WHY: rag_leg-level smoke proves retrieval; the business goal is the ANSWER
(sources + concrete unit-type figures) as the API produces it. Read-only with
respect to data stores; writes nothing. Uses .env credentials only.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QUESTIONS = {
    "camellia": "Các loại căn hộ tại Camellia có những loại nào, mỗi loại diện tích bao nhiêu?",
    "soleil": "Các loại căn hộ tại Soleil có những loại nào, mỗi loại diện tích bao nhiêu?",
}


async def run_one(project_key: str, query: str) -> None:
    from api.application.pipelines.conv_workflow import RagQueryPipelineConv

    pipe = RagQueryPipelineConv()
    payload = await pipe.run(
        query,
        f"smoke-{project_key}-{date.today().isoformat()}",
        date.today().isoformat(),
        [],
        project_key=project_key,
        device_id="smoke-device",
    )
    answer = str(payload.get("answer") or "")
    sources = payload.get("sources") or []
    images = payload.get("images") or []
    print(f"=== {project_key} === sources={len(sources)} images={len(images)} answer_chars={len(answer)}")
    for i, s in enumerate(sources[:4]):
        label = s.get("title") or s.get("label") or s.get("doc_id") if isinstance(s, dict) else str(s)
        print(f"  source[{i}]: {str(label)[:90]}")
    print("  answer excerpt:")
    for line in answer[:1200].splitlines():
        print(f"    {line}")


async def main() -> int:
    for project_key, query in QUESTIONS.items():
        await run_one(project_key, query)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
