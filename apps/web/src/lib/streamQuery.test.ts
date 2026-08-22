import { afterEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import type { FactEvidence, Source } from "@rag-ragre/contracts";
import { API_SSE_EVENTS } from "@rag-ragre/contracts";
import { streamQuery, type DoneMeta, type QueryStreamHandlers } from "@/lib/api";

// QA D1: the backend emits SOURCES/FACTS as object payloads
// (`{"sources": [...]}` / `{"facts": [...]}`, workflow.py) while some older
// paths and the `events:` batch line may carry bare arrays. These tests pin
// both shapes so the "Nguồn tài liệu" / "Sự kiện pháp lý" panels always render.

const SOURCE: Source = {
  doc_id: "soleil-chinh-sach-2.1",
  title: "Chính sách bán hàng The Soleil",
  section: "2.1",
  kind: "policy",
};

const FACT: FactEvidence = {
  fe_id: "fe-01",
  subject: "Giá căn 2PN",
  fields: { price_from: "5.2 tỷ" },
};

/** Builds a 200 SSE Response streaming the given raw frames once. */
function sseResponse(frames: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
}

interface RecordingHandlers extends QueryStreamHandlers {
  onSources: Mock<(sources: Source[]) => void>;
  onFacts: Mock<(facts: FactEvidence[]) => void>;
  onDone: Mock<(meta: DoneMeta) => void>;
}

function recordingHandlers(): RecordingHandlers {
  return {
    onSources: vi.fn(),
    onFacts: vi.fn(),
    onDone: vi.fn(),
  };
}

async function runStream(frames: string[], handlers: QueryStreamHandlers): Promise<void> {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse(frames)));
  try {
    await streamQuery({ query: "căn 2PN giá bao nhiêu" }, handlers);
  } finally {
    vi.unstubAllGlobals();
  }
}

describe("streamQuery — SOURCES event parsing", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("unwraps the backend object payload {sources: [...]} (QA D1)", async () => {
    const handlers = recordingHandlers();
    await runStream(
      [`event: ${API_SSE_EVENTS.SOURCES}\ndata: ${JSON.stringify({ sources: [SOURCE] })}\n\n`],
      handlers
    );
    expect(handlers.onSources).toHaveBeenCalledTimes(1);
    expect(handlers.onSources).toHaveBeenCalledWith([SOURCE]);
  });

  it("still accepts a bare array payload", async () => {
    const handlers = recordingHandlers();
    await runStream(
      [`event: ${API_SSE_EVENTS.SOURCES}\ndata: ${JSON.stringify([SOURCE])}\n\n`],
      handlers
    );
    expect(handlers.onSources).toHaveBeenCalledWith([SOURCE]);
  });

  it("yields an empty array when the object carries no sources key", async () => {
    const handlers = recordingHandlers();
    await runStream([`event: ${API_SSE_EVENTS.SOURCES}\ndata: {}\n\n`], handlers);
    expect(handlers.onSources).toHaveBeenCalledWith([]);
  });
});

describe("streamQuery — FACTS event parsing", () => {
  it("unwraps the backend object payload {facts: [...]} (QA D1)", async () => {
    const handlers = recordingHandlers();
    await runStream(
      [`event: ${API_SSE_EVENTS.FACTS}\ndata: ${JSON.stringify({ facts: [FACT] })}\n\n`],
      handlers
    );
    expect(handlers.onFacts).toHaveBeenCalledTimes(1);
    expect(handlers.onFacts).toHaveBeenCalledWith([FACT]);
  });

  it("still accepts a bare array payload", async () => {
    const handlers = recordingHandlers();
    await runStream(
      [`event: ${API_SSE_EVENTS.FACTS}\ndata: ${JSON.stringify([FACT])}\n\n`],
      handlers
    );
    expect(handlers.onFacts).toHaveBeenCalledWith([FACT]);
  });
});

describe("streamQuery — sources and facts in one stream", () => {
  it("parses both events from an events: batch line in order", async () => {
    const handlers = recordingHandlers();
    const batch = JSON.stringify([
      { event: API_SSE_EVENTS.SOURCES, data: { sources: [SOURCE] } },
      { event: API_SSE_EVENTS.FACTS, data: { facts: [FACT] } },
    ]);
    await runStream(
      [
        `event: ${API_SSE_EVENTS.ACK}\ndata: {}\n\n`,
        `events: ${batch}\n\n`,
        `event: ${API_SSE_EVENTS.DONE}\ndata: ${JSON.stringify({ trace_id: "t-1", latency_ms: 5 })}\n\n`,
      ],
      handlers
    );
    expect(handlers.onSources).toHaveBeenCalledWith([SOURCE]);
    expect(handlers.onFacts).toHaveBeenCalledWith([FACT]);
    expect(handlers.onDone).toHaveBeenCalledWith({ trace_id: "t-1", latency_ms: 5 });
    // Both evidence panels receive their data before the stream completes.
    expect(handlers.onSources.mock.invocationCallOrder[0]).toBeLessThan(
      handlers.onDone.mock.invocationCallOrder[0]
    );
    expect(handlers.onFacts.mock.invocationCallOrder[0]).toBeLessThan(
      handlers.onDone.mock.invocationCallOrder[0]
    );
  });

  it("keeps sources and facts when the frames arrive as chunked JSON payloads", async () => {
    const handlers = recordingHandlers();
    await runStream(
      [
        `event: ${API_SSE_EVENTS.SOURCES}\ndata: ${JSON.stringify({ sources: [SOURCE] })}\n\nevent: ${API_SSE_EVENTS.FACTS}\ndata: ${JSON.stringify({ facts: [FACT] })}\n\n`,
      ],
      handlers
    );
    expect(handlers.onSources).toHaveBeenCalledWith([SOURCE]);
    expect(handlers.onFacts).toHaveBeenCalledWith([FACT]);
  });
});
