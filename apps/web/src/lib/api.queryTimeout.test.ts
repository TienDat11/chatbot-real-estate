/**
 * Query-stream timeout contract: the whole POST /api/query SSE read must be
 * capped at >= 180s so a slow cold LightRAG first query (40s+ of silence
 * before the first token) survives, while a truly stalled stream still
 * terminates and drives the interrupted-stream retry UX.
 */
import { describe, expect, it } from "vitest";
import { QUERY_STREAM_TIMEOUT_MS } from "@/lib/api";

describe("QUERY_STREAM_TIMEOUT_MS", () => {
  it("is at least 180s (180_000 ms)", () => {
    expect(QUERY_STREAM_TIMEOUT_MS).toBeGreaterThanOrEqual(180_000);
  });
});
