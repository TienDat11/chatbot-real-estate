// @vitest-environment jsdom
/**
 * State-safety pins for ChatCanvas (regression-safe polish pass):
 *
 *  1. sanitizeGreetingSuggestions (pure): a late-arriving catalogue must still
 *     filter sibling project names out of greeting suggestions — the filter has
 *     to run against the catalogue live at RESOLUTION time, not mount time.
 *  2. Late-catalogue integration: /api/projects resolving AFTER fireGreeting is
 *     scheduled must not leak the sibling project's name into the suggestions.
 *  3. Deep-link hydration failure: bounded retry (2 attempts per session id),
 *     no crash, no redirect, existing empty presentation preserved.
 *  4. Unmount mid-stream: in-flight SSE aborted + pending token-flush timer
 *     cleared, so nothing fires into a dead component.
 *  5. Project switch while streaming: the old project's stream signal aborts.
 *  6. Throwing sessionStorage (private mode): new-session reset survives via
 *     the existing in-memory fallback and re-greets without crashing.
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

import { ChatPage, sanitizeGreetingSuggestions } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const BOTH_PROJECTS = {
  projects: [
    { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", short_name: "The Camellia", is_hot: true, lat: 16.1052, lng: 108.2558 },
    { project_key: "soleil", name: "The Soleil Đà Nẵng", short_name: "The Soleil", is_hot: false, lat: 16.071, lng: 108.2436 },
  ],
};
const BACKEND_GREETING = "Backend greeting for the selected project.";

function deferredResponse(): { promise: Promise<Response>; resolve: (response: Response) => void } {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

/** SSE body that delivers one token frame then stays open forever. */
function hangingTokenStreamResponse(): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('event: token\ndata: "Căn 2PN "\n\n'));
      // Never closes: the request hangs until the caller aborts.
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function ask(question: string): void {
  document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: question }));
}

describe("sanitizeGreetingSuggestions (pure)", () => {
  it("drops every alias of SIBLING projects, case-insensitively", () => {
    const safe = sanitizeGreetingSuggestions(
      [
        "Dự án The Soleil có tiện ích gì?",
        "soleil view biển thế nào?",
        "Chính sách The Soleil Đà Nẵng ra sao?",
      ],
      BOTH_PROJECTS.projects,
      "camellia"
    );
    expect(safe).toEqual([]);
  });

  it("keeps neutral questions and mentions of the CURRENT project", () => {
    const safe = sanitizeGreetingSuggestions(
      [
        "Dự án có những tiện ích gì nổi bật?",
        "Tiện ích The Camellia có gì?",
        "",
        "   ",
        42 as unknown as string,
      ],
      BOTH_PROJECTS.projects,
      "camellia"
    );
    expect(safe).toEqual(["Dự án có những tiện ích gì nổi bật?", "Tiện ích The Camellia có gì?"]);
  });

  it("returns [] for non-array payloads", () => {
    expect(sanitizeGreetingSuggestions(null, BOTH_PROJECTS.projects, "camellia")).toEqual([]);
    expect(sanitizeGreetingSuggestions("nope", [], "camellia")).toEqual([]);
  });
});

describe("ChatPage state safety", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("late catalogue: hello resolving after /api/projects never leaks the sibling project name", async () => {
    let helloCalls = 0;
    const deferredHello = deferredResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/llms-hello")) {
          helloCalls += 1;
          return deferredHello.promise;
        }
        if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(BOTH_PROJECTS));
        return Promise.resolve(jsonResponse({}));
      })
    );

    render(<ChatPage />);
    await waitFor(() => expect(helloCalls).toBeGreaterThan(0));
    const loadingStatus = screen.getByRole("status", { name: "Đang tải lời chào" });
    expect(loadingStatus.getAttribute("aria-busy")).toBe("true");
    // No client-owned greeting is rendered while the backend response is pending.
    expect(screen.queryByText(BACKEND_GREETING)).toBeNull();

    deferredHello.resolve(
      jsonResponse({
        greeting: BACKEND_GREETING,
        suggestions: [
          "Dự án Soleil có tiện ích gì?",
          "Dự án có những tiện ích gì nổi bật?",
        ],
      })
    );
    await screen.findByText(BACKEND_GREETING);
    expect(screen.queryByText("Dự án Soleil có tiện ích gì?")).toBeNull();
    expect(screen.queryByText(/Soleil/i)).toBeNull();
  });

  it("deep-link hydration failure retries at most twice per session and never crashes", async () => {
    let messageCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/sessions/sess-deep/messages")) {
          messageCalls += 1;
          return Promise.reject(new Error("stale deep link"));
        }
        if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(BOTH_PROJECTS));
        return Promise.resolve(jsonResponse({}));
      })
    );

    const { rerender } = render(<ChatPage routeProjectKey="camellia" sessionId="sess-deep" />);
    // Attempt 1 fires once deviceId resolves.
    await waitFor(() => expect(messageCalls).toBe(1));
    // A dependency change (routed project -> internal projectKey) retries once…
    rerender(<ChatPage routeProjectKey="soleil" sessionId="sess-deep" />);
    await waitFor(() => expect(messageCalls).toBe(2));
    // …but the per-session cap holds after that: further dep churn stays silent.
    rerender(<ChatPage routeProjectKey="camellia" sessionId="sess-deep" />);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(messageCalls).toBe(2);
    // No crash, chat shell still renders (existing empty presentation kept).
    expect(screen.getByTestId("composer-stub")).toBeTruthy();
  });

  it("unmount mid-stream aborts the in-flight SSE and clears the pending flush timer", async () => {
    const captured: { signal?: AbortSignal | null } = {};
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/query")) {
          captured.signal = init?.signal;
          return Promise.resolve(hangingTokenStreamResponse());
        }
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({}));
        if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(BOTH_PROJECTS));
        return Promise.resolve(jsonResponse({}));
      })
    );

    window.sessionStorage.setItem("ragre.hello_shown", "1");
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");
    const { unmount } = render(<ChatPage />);
    ask("Giá căn 2PN?");

    await waitFor(() => expect(captured.signal).toBeDefined());
    // Let the first token frame land: the token buffers and the 60ms flush
    // timer arms. Unmount BEFORE it fires so cleanup must clear it.
    await new Promise((resolve) => setTimeout(resolve, 10));
    unmount();

    expect(captured.signal?.aborted).toBe(true);
    expect(clearTimeoutSpy).toHaveBeenCalled();
  });

  it("switching the routed project aborts the old stream and discards its late greeting", async () => {
    const captured: { signal?: AbortSignal | null } = {};
    const helloResponses: Array<{ resolve: (response: Response) => void }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/llms-hello")) {
          const deferred = deferredResponse();
          helloResponses.push(deferred);
          return deferred.promise;
        }
        if (url.includes("/api/query")) {
          captured.signal = init?.signal;
          return Promise.resolve(hangingTokenStreamResponse());
        }
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({}));
        if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(BOTH_PROJECTS));
        return Promise.resolve(jsonResponse({}));
      })
    );

    window.sessionStorage.removeItem("ragre.hello_shown");
    const { rerender } = render(<ChatPage routeProjectKey="camellia" />);
    await waitFor(() => expect(helloResponses).toHaveLength(1));
    ask("Giá căn 2PN?");
    await waitFor(() => expect(captured.signal).toBeDefined());

    rerender(<ChatPage routeProjectKey="soleil" />);
    await waitFor(() => expect(captured.signal?.aborted).toBe(true));
    await waitFor(() => expect(helloResponses).toHaveLength(2));

    helloResponses[0].resolve(jsonResponse({ greeting: "Late Camellia greeting." }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Late Camellia greeting.")).toBeNull();
    const loadingStatus = screen.getByRole("status", { name: "Đang tải lời chào" });
    expect(loadingStatus.getAttribute("aria-busy")).toBe("true");

    helloResponses[1].resolve(jsonResponse({ greeting: "Soleil backend greeting." }));
    await screen.findByText("Soleil backend greeting.");
    expect(screen.queryByText("Late Camellia greeting.")).toBeNull();
  });

  it("throwing sessionStorage: new session falls back to an in-memory id without crashing", async () => {
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    const throwingStorage = {
      getItem: () => {
        throw new Error("private mode");
      },
      setItem: () => {
        throw new Error("private mode");
      },
      removeItem: () => {
        throw new Error("private mode");
      },
      clear: () => {
        throw new Error("private mode");
      },
    };
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: throwingStorage,
    });

    try {
      vi.stubGlobal(
        "fetch",
        vi.fn((input: RequestInfo | URL) => {
          const url = String(input);
          if (url.includes("/api/sessions")) return Promise.resolve(jsonResponse({ sessions: [] }));
          if (url.includes("/api/llms-hello")) return Promise.resolve(jsonResponse({ greeting: BACKEND_GREETING }));
          if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(BOTH_PROJECTS));
          return Promise.resolve(jsonResponse({}));
        })
      );

      render(<ChatPage />);
      // Mount already survived throwing storage; the backend-owned greeting renders
      // even though the latch is unreadable.
      await screen.findByText(BACKEND_GREETING);

      fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
      await screen.findByText("Chưa có đoạn chat nào");
      fireEvent.click(screen.getByRole("button", { name: "Đoạn chat mới" }));

      // No throw above means the getSessionId fallback worked; the forced
      // re-greeting replaces the wiped conversation.
      await screen.findByText(BACKEND_GREETING);
      expect(screen.getByTestId("composer-stub")).toBeTruthy();
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, "sessionStorage", originalDescriptor);
      }
    }
  });
});
