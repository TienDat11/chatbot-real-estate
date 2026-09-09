// @vitest-environment jsdom
/**
 * E2E-confirmed regression (deterministic 3/3): the dev proxy cut a slow
 * first Soleil query at ~38-44s (ERR_INCOMPLETE_CHUNKED_ENCODING) BEFORE the
 * `done` frame, so api.ts surfaced "Lỗi khi đọc luồng phản hồi." and the
 * partial answer was replaced by a bare error line.
 *
 * Pinned here:
 *  1. a mid-stream cut (tokens delivered, then the body errors) keeps the
 *     PARTIAL answer visible in the bubble and raises the antd Alert
 *     "Kết nối bị gián đoạn" with a "Thử lại" button;
 *  2. clicking "Thử lại" re-sends the SAME question through /api/query
 *     (asserted via the captured fetch bodies);
 *  3. no silent empty state: when the stream died before any token, the
 *     bubble shows the error copy instead of nothing.
 *
 * MessageList is deliberately NOT mocked — the unit under test is the
 * bubble-level resilience UX, so the real MessageBubble renders.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/Composer", () => ({ Composer: () => <div data-testid="composer-stub" /> }));
vi.mock("@/components/LeadForm", () => ({
  LeadForm: () => <div />,
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
}));
vi.mock("@/features/chat/ProjectPicker", () => ({ ProjectPicker: () => <div /> }));

import { ChatPage } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * SSE body that streams token frames, then the connection dies mid-stream
 * (controller.error after the queued frames are consumed) before any `done`
 * frame — the exact shape the browser sees when the proxy severs an
 * incomplete chunked response. The error is deferred to the next macrotask so
 * the reader delivers the queued tokens first.
 */
function sseCutAfterTokensResponse(): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('event: token\ndata: "Căn 2PN có diện tích "\n\n')
      );
      controller.enqueue(
        encoder.encode('event: token\ndata: "72m2, giá bán "\n\n')
      );
      setTimeout(() => controller.error(new Error("proxy severed mid-stream")), 0);
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** SSE body that dies BEFORE any token — no bytes at all. */
function sseCutEmptyResponse(): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.error(new Error("proxy severed before first token"));
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/**
 * SSE body that streams a partial answer then receives the backend's terminal
 * STREAM_TIMEOUT error frame (new total-deadline contract) followed by `done`.
 * The stream terminates cleanly — this is the server-side stall path, distinct
 * from a proxy mid-stream cut.
 */
function sseTimeoutFrameAfterTokensResponse(): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('event: token\ndata: "Căn 2PN diện tích "\n\n')
      );
      controller.enqueue(
        encoder.encode(
          'event: token\ndata: "72m2, giá từ "\n\n' +
            'event: error\ndata: {"code":"STREAM_TIMEOUT","message":"Máy chủ đang quá tải, chưa kịp trả lời. Vui lòng thử lại."}\n\n' +
            'event: done\ndata: {}\n\n'
        )
      );
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const ENDPOINT_PROJECTS = {
  projects: [
    { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
  ],
};

function ask(question: string): void {
  document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: question }));
}

describe("ChatPage interrupted-stream resilience", () => {
  let queryBodies: { query: string }[];

  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    // A stored choice skips the forced picker; hello latch suppresses the
    // fake greeting stream so the query flow is deterministic.
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    queryBodies = [];
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("keeps partial tokens + shows the retry Alert on a mid-stream cut, and Thử lại re-sends the same question", async () => {
    let queryCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/query")) {
          queryCalls += 1;
          queryBodies.push(JSON.parse(String(init?.body)));
          if (queryCalls === 1) return Promise.resolve(sseCutAfterTokensResponse());
          // Retry succeeds with a done frame.
          return Promise.resolve(
            new Response(
              'event: ack\ndata: {}\n\nevent: done\ndata: {"answer":"Đã trả lời xong","confidence":"HIGH"}\n\n',
              { status: 200, headers: { "Content-Type": "text/event-stream" } }
            )
          );
        }
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({}, 200));
        return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
      })
    );

    render(<ChatPage />);
    ask("Giá căn 2PN?");

    // THE PIN 1: the partial tokens survive the cut — the bubble keeps them.
    await screen.findByText(/Căn 2PN có diện tích 72m2, giá bán/);
    // The retry Alert is attached to the interrupted bubble.
    await waitFor(() => expect(screen.getByText("Kết nối bị gián đoạn")).toBeTruthy());

    // THE PIN 2: Thử lại re-sends the SAME question through the normal path.
    fireEvent.click(screen.getByRole("button", { name: /Thử lại/i }));
    await waitFor(() => expect(queryBodies.length).toBe(2));
    expect(queryBodies[1].query).toBe("Giá căn 2PN?");
    // The retried stream completes and replaces the partial body.
    await screen.findByText("Đã trả lời xong");
  });

  it("shows the error copy (no silent empty state) when the stream dies before any token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/query")) return Promise.resolve(sseCutEmptyResponse());
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({}, 200));
        return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
      })
    );

    render(<ChatPage />);
    ask("Giá căn 2PN?");

    // The error copy fills the bubble instead of an empty placeholder.
    await screen.findByText(/Lỗi khi đọc luồng phản hồi/);
    expect(screen.getByText("Kết nối bị gián đoạn")).toBeTruthy();
  });

  it("keeps partial tokens + re-enables the composer after the backend STREAM_TIMEOUT error frame", async () => {
    let queryCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/query")) {
          queryCalls += 1;
          queryBodies.push(JSON.parse(String(init?.body)));
          // First query stalls server-side: partial tokens then the terminal
          // STREAM_TIMEOUT error frame (no mid-stream network cut).
          return Promise.resolve(sseTimeoutFrameAfterTokensResponse());
        }
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({}, 200));
        return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
      })
    );

    render(<ChatPage />);
    ask("Giá căn 2PN?");

    // THE PIN 1: partial tokens survive the terminal timeout frame.
    await screen.findByText(/Căn 2PN diện tích 72m2, giá từ/);
    // The retry Alert marks the interrupted bubble.
    await waitFor(() => expect(screen.getByText("Kết nối bị gián đoạn")).toBeTruthy());

    // THE PIN 2: the composer is re-enabled (streaming reset) — the next
    // question is accepted instead of being swallowed by the streaming guard.
    ask("Căn 3PN giá bao nhiêu?");
    await waitFor(() => expect(queryBodies.length).toBe(2));
    expect(queryBodies[1].query).toBe("Căn 3PN giá bao nhiêu?");
  });
});
