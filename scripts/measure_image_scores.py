"""Measure the raw image-score distribution under gemini-embedding-001.

WHY: the kind-aware gates (threshold/same_kind_margin/cross_kind_margin) were
tuned on the text-embedding-v4 score scale. This script dumps every candidate
score per benchmark query (floor=0, margin=1 -> keep-all) so the new gate
values are chosen from data, not guessed.
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

QUERIES = {
    "PAYMENT": "Phương thức thanh toán mua hàng ở camellia như nào",
    "MARKET": "tổng quan thị trường bất động sản Đà Nẵng 2026",
    "FLOORPLAN_FULL": "mặt bằng dự án The Camellia",
    "FLOORPLAN_SHORT": "mặt bằng tổng thể dự án",
    "AIRPORT": "sân bay Đà Nẵng cách dự án bao xa",
    "UNIT_CH03": "mặt bằng căn hộ CH-03 view biển",
}

SOLEIL_QUERIES = {
    "SOLEIL_FLOORPLAN": "mặt bằng dự án The Soleil",
    "SOLEIL_UNIT": "căn hộ 2 phòng ngủ Soleil diện tích",
    "SOLEIL_AMENITY": "tiện ích nội khu Soleil",
}


async def _measure(query: str, project_key: str) -> list[dict]:
    from api.application.services import sql_leg
    from api.application.services.image_search import search_images

    try:
        sql_leg._ro_pool = None
        return await asyncio.wait_for(
            search_images(
                query,
                project_key=project_key,
                threshold=0.0,
                margin=1.0,
            ),
            timeout=30,
        )
    finally:
        await sql_leg.close_ro_pool()


async def main() -> int:
    for label, query in QUERIES.items():
        rows = await asyncio.wait_for(_measure(query, "camellia"), timeout=40)
        head = "; ".join(
            f"{r['image_id']}({r['kind']},{r['score']:.4f})" for r in rows[:10]
        )
        print(f"{label}: {len(rows)} rows")
        print(f"  {head}")
    for label, query in SOLEIL_QUERIES.items():
        rows = await asyncio.wait_for(_measure(query, "soleil"), timeout=40)
        head = "; ".join(
            f"{r['image_id']}({r['kind']},{r['score']:.4f})" for r in rows[:10]
        )
        print(f"{label}: {len(rows)} rows")
        print(f"  {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
