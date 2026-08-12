# Rewrite + Route + Spec — few-shot cho LLM rewrite (deepseek-v4-flash, JSON mode)
# Plan §4.2: 1 call duy nhất trả { rewritten_query, routing, sql_spec, keywords, high_stakes, as_of }
# - Multi-turn: resolve tham chiếu từ history (≤4 turn) — query ra phải SELF-CONTAINED.
# - Parse số tiếng Việt: "2,85 tỷ" -> 2850000000; "dưới 2 tỷ" -> <= 2000000000.
# - structured_path ∈ {spec, nl2sql, none}; nl2sql CHỈ khi intent aggregate/compare
#   (người máy detector deterministic xác nhận sau).

## Example 1 — Affordability (giá trị nhất)
History: []
Query: "tôi có 2 tỉ mua được nhà nào?"
```json
{
  "rewritten_query": "Tìm căn hộ có số tiền cần trả trước không vượt quá 2 tỷ đồng",
  "routing": {"needs_rag": false, "needs_sql": true, "structured_path": "spec"},
  "sql_spec": {
    "subject_type": "unit",
    "source": "v_unit_offers",
    "filters": [{"field": "required_down_payment_vnd", "op": "<=", "value": 2000000000}],
    "order_by": {"field": "required_down_payment_vnd", "dir": "asc"},
    "limit": 10
  },
  "hl_keywords": [], "ll_keywords": ["trả trước", "2 tỷ"],
  "high_stakes": false, "as_of": null
}
```

## Example 2 — Hard filter value
History: []
Query: "căn nào diện tích trên 80m2 giá dưới 5 tỷ?"
```json
{
  "rewritten_query": "Tìm căn hộ diện tích lớn hơn 80 mét vuông và giá dưới 5 tỷ đồng",
  "routing": {"needs_rag": false, "needs_sql": true, "structured_path": "spec"},
  "sql_spec": {
    "subject_type": "unit", "source": "facts",
    "filters": [
      {"field": "area_m2", "op": ">", "value": 80},
      {"field": "price_vnd", "op": "<", "value": 5000000000}
    ],
    "order_by": {"field": "price_vnd", "dir": "asc"},
    "limit": 10
  },
  "hl_keywords": [], "ll_keywords": ["diện tích", "giá"],
  "high_stakes": false, "as_of": null
}
```

## Example 3 — Aggregate (điều hướng NL2SQL, detector xác nhận sau)
History: []
Query: "có bao nhiêu căn dưới 2 tỷ?"
```json
{
  "rewritten_query": "Đếm số lượng căn hộ có giá dưới 2 tỷ đồng",
  "routing": {"needs_rag": false, "needs_sql": true, "structured_path": "nl2sql"},
  "sql_spec": null,
  "hl_keywords": [], "ll_keywords": ["bao nhiêu căn", "2 tỷ"],
  "high_stakes": false, "as_of": null
}
```

## Example 4 — Legal (RAG-only)
History: []
Query: "cầm cố quyền sử dụng đất có hợp pháp không?"
```json
{
  "rewritten_query": "Cầm cố quyền sử dụng đất có được pháp luật ghi nhận không? Rủi ro pháp lý như thế nào?",
  "routing": {"needs_rag": true, "needs_sql": false, "structured_path": "none"},
  "sql_spec": null,
  "hl_keywords": ["cầm cố", "quyền sử dụng đất"], "ll_keywords": ["cầm cố", "quyền sử dụng đất", "rủi ro", "vô hiệu"],
  "high_stakes": true, "as_of": null
}
```

## Example 5 — Chính sách vay (spec theo policy)
History: []
Query: "căn A10-01 vay bank nào được trả trước 25%?"
```json
{
  "rewritten_query": "Chính sách vay của căn hộ A10-01: các ngân hàng có mức trả trước 25%",
  "routing": {"needs_rag": false, "needs_sql": true, "structured_path": "spec"},
  "sql_spec": {
    "subject_type": "unit", "source": "facts",
    "filters": [
      {"field": "subject_key", "op": "=", "value": "unit:tower-a/A10-01"},
      {"field": "deposit_pct", "op": "=", "value": 25}
    ],
    "order_by": {"field": "deposit_pct", "dir": "asc"},
    "limit": 10
  },
  "hl_keywords": [], "ll_keywords": ["trả trước", "25%"],
  "high_stakes": false, "as_of": null
}
```

## LUẬT CHƠI
- Nếu không thể route → `structured_path: "none"`, `needs_rag: true` (fallback an toàn).
- Số luôn là số nguyên (vnd) hoặc float (pct); KHÔNG để chữ "tỷ" trong value.
- `high_stakes` = true nếu query chứa keyword: cầm cố, thế chấp, chuyển nhượng, công chứng,
  quy hoạch, thuế, sổ đỏ, giải chấp, tranh chấp, ủy quyền, kê biên, hiệu lực.
- `as_of`: null (mặc định hôm nay) hoặc ISO date.
