/**
 * Bearer-seam contract tests for POST /api/query (sales-shell gap fix):
 *  1. an installed token provider turns into exactly one
 *     `Authorization: Bearer <token>` request header;
 *  2. absent provider / null-or-empty token / throwing provider all send NO
 *     Authorization header while the anonymous payload (anon_token) and the
 *     SSE read loop stay byte-identical to the historical behavior.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  setQueryAuthTokenProvider,
  streamQuery,
  type QueryStreamHandlers,
} from "@/lib/api";

/** Minimal SSE replay ending in `done` so streamQuery runs to completion. */
function sseDoneResponse(): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('event: done\ndata: {"trace_id":"t1","latency_ms":1}\n\n')
      );
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function captureQueryFetch(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(() => Promise.resolve(sseDoneResponse()));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function firstCallHeaders(fetchMock: ReturnType<typeof vi.fn>): Record<string, string> {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return (init.headers ?? {}) as Record<string, string>;
}

const NOOP_HANDLERS: QueryStreamHandlers = {};

afterEach(() => {
  // Never leak a provider into another test (per-file isolation aside).
  setQueryAuthTokenProvider(null);
  vi.unstubAllGlobals();
});

describe("streamQuery bearer seam", () => {
  it("sends no Authorization header without a provider and keeps the anon payload", async () => {
    const fetchMock = captureQueryFetch();

    let doneFired = false;
    await streamQuery(
      { query: "Giá 2PN?", device_id: "dev1", anon_token: "anon_123" },
      { ...NOOP_HANDLERS, onDone: () => (doneFired = true) }
    );

    const headers = firstCallHeaders(fetchMock);
    expect(headers.Authorization).toBeUndefined();
    // Historical anonymous identity payload untouched.
    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
    expect(body.anon_token).toBe("anon_123");
    expect(body.query).toBe("Giá 2PN?");
    expect(doneFired).toBe(true);
  });

  it("attaches Authorization: Bearer when the provider resolves a token", async () => {
    const fetchMock = captureQueryFetch();
    setQueryAuthTokenProvider(async () => "idp_sales_tok_9");

    await streamQuery({ query: "q", anon_token: "anon_1" }, NOOP_HANDLERS);

    expect(firstCallHeaders(fetchMock).Authorization).toBe("Bearer idp_sales_tok_9");
  });

  it("omits the header when the provider resolves null or empty", async () => {
    const fetchNull = captureQueryFetch();
    setQueryAuthTokenProvider(async () => null);
    await streamQuery({ query: "q", anon_token: "a" }, NOOP_HANDLERS);
    expect(firstCallHeaders(fetchNull).Authorization).toBeUndefined();

    const fetchEmpty = captureQueryFetch();
    setQueryAuthTokenProvider(() => "");
    await streamQuery({ query: "q", anon_token: "a" }, NOOP_HANDLERS);
    expect(firstCallHeaders(fetchEmpty).Authorization).toBeUndefined();
  });

  it("degrades a throwing provider to no header and still completes the stream", async () => {
    const fetchMock = captureQueryFetch();
    setQueryAuthTokenProvider(async () => {
      throw new Error("firebase unavailable");
    });

    let doneFired = false;
    await streamQuery(
      { query: "q", anon_token: "a" },
      { ...NOOP_HANDLERS, onDone: () => (doneFired = true) }
    );

    expect(firstCallHeaders(fetchMock).Authorization).toBeUndefined();
    expect(doneFired).toBe(true);
  });
});
