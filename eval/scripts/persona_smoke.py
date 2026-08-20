"""Story 4.7 §8.3 — Persona live smoke: 5 hội thoại 3-turn thật.

Cần keys/PG (LLM_API_KEY, RERANK_API_KEY, EMBEDDING_API_KEY, POSTGRES_*). Sẽ ghi
eval/reports/persona-YYYY-MM.md kèm đánh giá chủ quan 1-5 sao (người chạy chấm).
Đây là live LLM — KHÔNG chạy khi API quota đang cạn.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.application.pipelines.conv_workflow import RagQueryPipelineConv  # noqa: E402

# 5 hội thoại 3-turn (mỗi hàng = [query, history_user, history_assistant] trước lượt hiện tại)
SCENARIOS = [
    {
        "id": "conv-01",
        "name": "pricing -> budget -> tư vấn",
        "turns": [
            {"query": "cho hỏi căn 2PN giá bao nhiêu", "history": []},
            {"query": "tụi tui có khoảng 3 tỷ thôi", "history": [
                {"role": "user", "content": "cho hỏi căn 2PN giá bao nhiêu"},
                {"role": "assistant", "content": "Căn 2PN The Camellia giá định hướng khởi điểm [fe-xxx]."},
            ]},
            {"query": "để lại số được chuyên viên gọi nhé", "history": [
                {"role": "user", "content": "cho hỏi căn 2PN giá bao nhiêu"},
                {"role": "assistant", "content": "Căn 2PN giá định hướng [fe-xxx]."},
                {"role": "user", "content": "tụi tui có khoảng 3 tỷ thôi"},
                {"role": "assistant", "content": "Với 3 tỷ anh/chị xem nhóm [fe-xxx]."},
            ]},
        ],
    },
    {
        "id": "conv-02",
        "name": "pháp lý -> steer nhẹ",
        "turns": [
            {"query": "dự án đã có sổ chưa", "history": []},
            {"query": "sợ pháp lý lắm", "history": [
                {"role": "user", "content": "dự án đã có sổ chưa"},
                {"role": "assistant", "content": "GCN QSDĐ CT09441, QĐ 254 [fe-xxx]."},
            ]},
            {"query": "vậy để em nhờ chuyên viên gọi tư vấn", "history": [
                {"role": "user", "content": "dự án đã có sổ chưa"},
                {"role": "assistant", "content": "GCN CT09441, QĐ 254 [fe-xxx]."},
                {"role": "user", "content": "sợ pháp lý lắm"},
                {"role": "assistant", "content": "QĐ 254 31/01/2024, HS SDD 11,09 [fe-xxx]."},
            ]},
        ],
    },
    {
        "id": "conv-03",
        "name": "hỏi đối thủ giá rẻ -> so sánh",
        "turns": [
            {"query": "bên anh đắt hơn bên kia nhiều không", "history": []},
            {"query": "mà vị trí bên này có gì đặc biệt", "history": [
                {"role": "user", "content": "bên anh đắt hơn bên kia nhiều không"},
                {"role": "assistant", "content": "Vị trí Sơn Trà, giao lộ Lê Văn Lương - Lê Đức Thọ, HTLS 0% [fe-xxx]"},
            ]},
            {"query": "ok cho em xem thực tế nhé", "history": [
                {"role": "user", "content": "bên anh đắt hơn bên kia nhiều không"},
                {"role": "assistant", "content": "Vị trí giao lộ Lê Văn Lương - Lê Đức Thọ [fe-xxx]"},
                {"role": "user", "content": "mà vị trí bên này có gì đặc biệt"},
                {"role": "assistant", "content": "Bán từ tầng 3A, view thoáng [fe-xxx]"},
            ]},
        ],
    },
    {
        "id": "conv-04",
        "name": "đầu tư cho thuê",
        "turns": [
            {"query": "mua studio invest cho thuê được không", "history": []},
            {"query": "ai vận hành tòa nhà vậy", "history": [
                {"role": "user", "content": "mua studio invest cho thuê được không"},
                {"role": "assistant", "content": "Studio 81 căn, quản lý vận hành dự kiến CBRE/PMC [fe-xxx]"},
            ]},
            {"query": "em muốn để lại số nhận tư vấn", "history": [
                {"role": "user", "content": "mua studio invest cho thuê được không"},
                {"role": "assistant", "content": "Studio 81 + 1.5PN 82 căn, view biển/nội khu [fe-xxx]"},
                {"role": "user", "content": "ai vận hành tòa nhà vậy"},
                {"role": "assistant", "content": "CBRE/PMC hoặc tương đương [fe-xxx]"},
            ]},
        ],
    },
    {
        "id": "conv-05",
        "name": "nhỏ tuổi vay -> gợi ý",
        "turns": [
            {"query": "tôi 60 tuổi có vay được không", "history": []},
            {"query": "vậy trả thảnh thơi được không", "history": [
                {"role": "user", "content": "tôi 60 tuổi có vay được không"},
                {"role": "assistant", "content": "Tuổi tối đa tùy ngân hàng, em kết nối chuyên viên tư vấn"},
            ]},
            {"query": "ok cho em đặt cọc giữ chỗ", "history": [
                {"role": "user", "content": "tôi 60 tuổi có vay được không"},
                {"role": "assistant", "content": "Tùy chính sách ngân hàng, em hỗ trợ kết nối."},
                {"role": "user", "content": "vậy trả thảnh thơi được không"},
                {"role": "assistant", "content": "Thảnh thơi: vốn tự có 50% nhận nhà, CK 5% tại sàn [fe-xxx]"},
            ]},
        ],
    },
]


async def run_scenario(pipeline, scenario) -> list[dict]:
    turns_out = []
    for t in scenario["turns"]:
        res = await pipeline.run(query=t["query"], session_id=scenario["id"], history=t["history"])
        turns_out.append({
            "query": t["query"],
            "conv_state": res.get("conv_state"),
            "answer": (res.get("answer") or "")[:400],
        })
    return turns_out


async def main() -> None:
    pipeline = RagQueryPipelineConv()
    report_lines = [f"# Persona smoke {date.today().isoformat()}", ""]
    report_lines.append("> Live LLM — chấm 1-5 sao cho từng hội thoại (persona, chính xác, tự nhiên).")
    for s in SCENARIOS:
        print(f"--- {s['id']} {s['name']} ---")
        turns = await run_scenario(pipeline, s)
        report_lines.append(f"## {s['id']} — {s['name']}")
        for i, t in enumerate(turns, 1):
            print(f"  [turn{i}] {t['conv_state']}: {t['query']}")
            print(f"          {t['answer']}")
            report_lines.append(f"### Turn {i} (state={t['conv_state']})")
            report_lines.append(f"- Query: {t['query']}")
            report_lines.append(f"- Answer: {t['answer']}")
        report_lines.append("- Sao (1-5): ___")
        report_lines.append("")

    out = REPO_ROOT / "eval" / "reports" / f"persona-{date.today():%Y-%m-%d}.md"
    out.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
