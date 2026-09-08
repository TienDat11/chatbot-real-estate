// @vitest-environment jsdom
/**
 * R4 (FR-19) pins: switch-time hydration guards.
 *
 * Pinned here:
 *  1. Rapid switch ordering: a Camellia->Soleil switch while the deep-link
 *    hydration fetch is still in flight must NOT land the old transcript — the
 *    Soleil greeting survives, no cross-project contamination, and the URL/
 *    storage carry a FRESH session id.
 *  2. Empty-session hydration guard: deep-link hydration resolving an EMPTY
 *    transcript (session with no server messages) at the same epoch must not
 *    wipe bucket content the user produced meanwhile.
 *  3. handleNewSession bumps the epoch: stale transcript cannot resurrect.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const routerMock = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => routerMock }));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({
  MessageList: (props: {
    messages?: Array<{ id: string; content: string; role: string }>;
    suggestions?: string[];
  }) => (
    <div data-testid="messages-stub">
      {(props.suggestions ?? []).map((s) => (
        <button key={s} type="button">{s}</button>
      ))}
      {(props.messages ?? []).map((m) => (
        <div key={m.id} data-testid="canvas-message">{m.content}</div>
      ))}
    </div>
  ),
}));
vi.mock("@/components/Composer", () => ({ Composer: () => <div data-testid="composer-stub" /> }));
vi.mock("@/components/LeadForm", () => ({
  LeadForm: () => <div />,
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
}));
vi.mock("@/features/chat/ProjectPicker", () => ({
  ProjectPicker: (props: {
    open?: boolean;
    projects?: Array<{ project_key: string; name: string }>;
    onSelect?: (key: string) => void;
  }) =>
    props.open ? (
      <div role="dialog">
        {(props.projects ?? []).map((p) => (
          <button key={p.project_key} type="button" onClick={() => props.onSelect?.(p.project_key)}>
            {p.name}
          </button>
        ))}
      </div>
    ) : null,
}));

import { ChatPage } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { SESSION_KEY } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const CAMELLIA_TRANSCRIPT = {
  messages: [
    { role: "user", content: "Câu hỏi CAMELLIA cũ trong lịch sử", meta: null, created_at: "t0" },
    { role: "assistant", content: "Trả lời CAMELLIA cũ", meta: null, created_at: "t1" },
  ],
};

describe("ChatPage switch-time hydration guards (R4)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    routerMock.push.mockClear();
    routerMock.replace.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("Camellia->Soleil while hydration is in flight: greeting survives, no stale transcript lands, fresh sessionId in storage+URL", async () => {
    let resolveTranscript!: (response: Response) => void;
    const transcriptDeferred = new Promise<Response>((resolve) => { resolveTranscript = resolve; });
    const soleilGreetingPayload = {
      greeting: "Chào mừng đến với The Soleil!",
      suggestions: [],
      images: [],
      videos: [],
    };
    const beforeSwitchSid = "sess-cam-old";
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes(`/api/sessions/${beforeSwitchSid}/messages`)) return transcriptDeferred;
        if (url.includes("/api/llms-hello")) return Promise.resolve(jsonResponse(soleilGreetingPayload));
        if (url.includes("/api/projects")) {
          return Promise.resolve(jsonResponse({
            projects: [
              { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
              { project_key: "soleil", name: "The Soleil Đà Nẵng", is_hot: false, lat: 16.071, lng: 108.2436 },
            ],
          }));
        }
        return Promise.resolve(jsonResponse({}, 200));
      })
    );

    window.sessionStorage.setItem(SESSION_KEY, beforeSwitchSid);
    render(<ChatPage routeProjectKey="camellia" sessionId={beforeSwitchSid} />);

    // Deep-link hydration request is IN FLIGHT when the user switches project.
    fireEvent.click(screen.getByRole("button", { name: /Đổi dự án/i }));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /The Soleil Đà Nẵng/i }));

    // The fresh greeting wins the canvas first.
    await waitFor(() =>
      expect(screen.getAllByTestId("canvas-message").some((n) => n.textContent === "Chào mừng đến với The Soleil!")).toBe(true)
    );
    // Fresh session minted by the switch: storage moved off the old deep link,
    // and the pushed route carries exactly that new canonical sessionId.
    const storedSid = window.sessionStorage.getItem(SESSION_KEY);
    expect(storedSid).not.toBeNull();
    expect(storedSid).not.toBe(beforeSwitchSid);

    // THEN the stale Camellia transcript resolves: it must be dropped entirely
    // (epoch guard), leaving the Soleil greeting intact.
    await act(async () => {
      resolveTranscript(jsonResponse(CAMELLIA_TRANSCRIPT));
    });
    await waitFor(() => {
      const texts = screen.getAllByTestId("canvas-message").map((n) => n.textContent ?? "");
      expect(texts.some((t) => t.includes("CAMELLIA cũ"))).toBe(false);
      expect(texts).toContain("Chào mừng đến với The Soleil!");
    });
    // The old session's messages were never written into ANY bucket surface:
    // nothing camellia-scoped became visible after resolution.
    expect(String(routerMock.push.mock.calls.at(-1)?.[0])).toBe(
      `/project/soleil?sessionId=${storedSid}`
    );
  });

  it("deep-link hydration resolving EMPTY does not wipe user turns sent meanwhile (same epoch)", async () => {
    let resolveEmpty!: (response: Response) => void;
    const emptyDeferred = new Promise<Response>((resolve) => { resolveEmpty = resolve; });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/messages")) return emptyDeferred;
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({ anon_token: "tok_race_1" }));
        if (url.includes("/api/query")) {
          const encoder = new TextEncoder();
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify({ trace_id: "t", latency_ms: 1, confidence: "HIGH", requires_review: false, answer: "Trả lời OK" })}\n\n`));
              controller.close();
            },
          });
          return Promise.resolve(new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
        }
        return Promise.resolve(jsonResponse({}, 200));
      })
    );

    render(<ChatPage sessionId="sess-empty-1" />);
    // Send BEFORE hydration resolves: handleSend runs at the same epoch and
    // fills the previously-empty bucket with a real exchange.
    fireEvent(window.document, new CustomEvent(ASK_EVENT, { detail: "Giá 2PN bao nhiêu?" }));
    await waitFor(() =>
      expect(screen.getAllByTestId("canvas-message").some((n) => n.textContent === "Giá 2PN bao nhiêu?")).toBe(true)
    );

    // Stale backend says this session has ZERO messages.
    await act(async () => {
      resolveEmpty(jsonResponse({ messages: [] }));
    });
    // The empty-write guard keeps the user's turn instead of replacing it [].
    await waitFor(() =>
      expect(screen.getAllByTestId("canvas-message").some((n) => n.textContent === "Giá 2PN bao nhiêu?")).toBe(true)
    );
  });

  it("handleNewSession bumps the epoch so a stale transcript cannot resurrect after the reset", async () => {
    let resolveTranscript!: (response: Response) => void;
    const transcriptDeferred = new Promise<Response>((resolve) => { resolveTranscript = resolve; });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/messages")) return transcriptDeferred;
        if (url.includes("/api/llms-hello")) {
          return Promise.resolve(jsonResponse({ greeting: "Lời chào mới.", suggestions: [], images: [], videos: [] }));
        }
        return Promise.resolve(jsonResponse({}, 200));
      })
    );

    render(<ChatPage sessionId="sess-reset-1" />);
    // New session reset happens while hydration is still pending (the reset
    // action lives inside the history drawer).
    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    fireEvent.click(await screen.findByRole("button", { name: "Đoạn chat mới" }));
    await act(async () => {
      resolveTranscript(jsonResponse(CAMELLIA_TRANSCRIPT));
    });
    await waitFor(() =>
      expect(screen.getAllByTestId("canvas-message").some((n) => n.textContent?.includes("CAMELLIA cũ"))).toBe(false)
    );
  });
});
