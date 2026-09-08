import { afterEach, describe, expect, it, vi } from "vitest";
import { setQueryAuthTokenProvider, streamQuery } from "@/lib/api";

function responseFor(frames: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(frames));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

afterEach(() => {
  setQueryAuthTokenProvider(null);
  vi.unstubAllGlobals();
});

describe("streamQuery metadata envelopes", () => {
  it("preserves sources and facts object envelopes", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(responseFor(
      'event: sources\ndata: {"sources":[{"source_id":"s1","title":"Hợp đồng"}]}\n\n' +
      'event: facts\ndata: {"facts":[{"subject":"Pháp lý","value":"Đủ hồ sơ"}]}\n\n'
    ))));
    const sources: unknown[] = [];
    const facts: unknown[] = [];
    await streamQuery(
      { query: "q", session_id: "session-1", project_key: "camellia" },
      { onSources: (value) => sources.push(...value), onFacts: (value) => facts.push(...value) }
    );
    expect(sources).toEqual([{ source_id: "s1", title: "Hợp đồng" }]);
    expect(facts).toEqual([{ subject: "Pháp lý", value: "Đủ hồ sơ" }]);
    const body = JSON.parse(String((vi.mocked(fetch).mock.calls[0][1] as RequestInit).body));
    expect(body).toMatchObject({ session_id: "session-1", project_key: "camellia" });
  });
});
