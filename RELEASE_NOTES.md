# Release notes

- The customer-generation policy directs useful qualitative same-project overviews when FACT_EVIDENCE is empty and prohibits unsupported numeric claims, including numbers found only on RAG price boards. Numeric grounding remains enforced by the unchanged output guard; model compliance is not deterministic.
- Exact-unit answers filter presented facts to the requested unit and project, and images to matching project and unit provenance; generation and verification retain complete retrieved evidence.
- Exact-unit price requests without a same-project exact-unit price fact return a LOW-confidence, review-required unavailable answer; generic unit-type estimates are not used.
- P0-03A: selected authority now follows nonblank serialized prompt evidence and verified current registry rows; blank or unverified rows fail closed. `audit.chunk_ids` remains retrieval-only. No P0-03B/C publication or financial policy is changed.
