// @vitest-environment jsdom
/**
 * ChatPage bearer wiring (sales-shell quota gap): while ChatCanvas is mounted
 * the Firebase ID-token provider is installed, so an authenticated send ships
 * `Authorization: Bearer <token>` on /query and the server's authenticated
 * quota snapshot renders the unlimited badge; a signed-out provider (null)
 * sends no Authorization header and the anon_token flow is unchanged.
 *
 * The provider module itself is mocked — the unit under test is the ChatPage
 * registration + api.ts header merge, not firebase/auth.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

// ChatCanvas reads useRouter for routed project switches; these tests mount
// without a Next app context, so the router is a structural no-op double.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

const firebaseQueryAuthTokenMock = vi.hoisted(() => vi.fn());

vi.mock("@/features/auth/queryAuthToken", () => ({
  firebaseQueryAuthToken: firebaseQueryAuthTokenMock,
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({ MessageList: () => <div data-testid="messages-stub" /> }));
vi.mock("@/components/Composer", () => ({
  Composer: (props: { disabled?: boolean; value?: string }) => (
    <div>
      <input aria-label="Câu hỏi" disabled={props.disabled} value={props.value ?? ""} readOnly />
    </div>
  ),
}));
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: () => null,
}));

import { ChatPage } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";
import { setQueryAuthTokenProvider } from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const ENDPOINT_PROJECTS = {
  projects: [
    { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
  ],
};

/** Fetch double routing non-query endpoints and recording every /query call. */
function stubFetchWithQueryCapture(queryFrames: string[]): { queryCalls: { init: RequestInit }[] } {
  const queryCalls: { init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
      if (url.includes("/api/anon/token")) {
        return Promise.resolve(jsonResponse({ anon_token: "tok_minted_1" }));
      }
      if (url.includes("/api/query")) {
        queryCalls.push({ init: init ?? {} });
        return Promise.resolve(sseResponse(queryFrames));
      }
      return Promise.resolve(jsonResponse({}, 200));
    })
  );
  return { queryCalls };
}

function ask(question: string): void {
  document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: question }));
}

describe("ChatPage /query bearer wiring", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    firebaseQueryAuthTokenMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    setQueryAuthTokenProvider(null);
    vi.unstubAllGlobals();
  });

  it("an authenticated send carries Bearer <id token> and the badge reads unlimited", async () => {
    firebaseQueryAuthTokenMock.mockResolvedValue("idp_sales_tok_1");
    const { queryCalls } = stubFetchWithQueryCapture([
      `event: ack\ndata: ${JSON.stringify({
        quota: { used_turns: 0, remaining_turns: null, cap: null, is_authenticated: true, bonus_granted: 0 },
      })}\n\n`,
      `event: done\ndata: ${JSON.stringify({
        trace_id: "t1",
        latency_ms: 4,
        answer: "OK",
        quota: { used_turns: 1, remaining_turns: null, cap: null, is_authenticated: true, bonus_granted: 0 },
      })}\n\n`,
    ]);

    render(<ChatPage />);
    ask("Chính sách bán hàng?");

    // The registered provider resolved a fresh token per send...
    await waitFor(() =>
      expect(firebaseQueryAuthTokenMock).toHaveBeenCalled()
    );
    // ...and the outgoing /query carried it as a Bearer credential.
    await waitFor(() => expect(queryCalls.length).toBeGreaterThan(0));
    const headers = (queryCalls[0].init.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer idp_sales_tok_1");
    // Server-side authenticated snapshot drives the unlimited badge.
    await waitFor(() => expect(screen.getByText("Không giới hạn")).toBeTruthy());
  });

  it("a signed-out provider sends no Authorization header; anon_token flow unchanged", async () => {
    firebaseQueryAuthTokenMock.mockResolvedValue(null);
    const { queryCalls } = stubFetchWithQueryCapture([
      `event: ack\ndata: ${JSON.stringify({
        quota: { used_turns: 1, remaining_turns: 2, cap: 3, is_authenticated: false, bonus_granted: 0 },
        anon_token: "tok_sse_1",
      })}\n\n`,
      `event: done\ndata: ${JSON.stringify({
        trace_id: "t2",
        latency_ms: 4,
        answer: "FINAL",
        quota: { used_turns: 1, remaining_turns: 2, cap: 3, is_authenticated: false, bonus_granted: 0 },
      })}\n\n`,
    ]);

    render(<ChatPage />);
    ask("Giá căn 2PN?");

    await waitFor(() => expect(queryCalls.length).toBeGreaterThan(0));
    const headers = (queryCalls[0].init.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    // Anonymous identity still travels in the body (minted on first turn).
    const body = JSON.parse(String(queryCalls[0].init.body));
    expect(body.anon_token).toBe("tok_minted_1");
    // Anonymous quota badge path untouched.
    await waitFor(() => expect(screen.getByText("Còn 2 lượt")).toBeTruthy());
    expect(screen.queryByText("Không giới hạn")).toBeNull();
  });
});
