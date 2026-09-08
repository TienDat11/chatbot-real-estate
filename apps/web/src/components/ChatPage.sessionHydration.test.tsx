// @vitest-environment jsdom
/**
 * Customer deep-link hydration must survive a re-run of the hydration effect.
 *
 * The verified bug: the effect latched `hydratedSessionRef` AT DISPATCH and its
 * cleanup cancelled the in-flight fetch. Under React StrictMode's double-mount
 * (Next 16 defaults reactStrictMode true) — or any dep churn — run #1's cleanup
 * cancelled its fetch while run #2 early-returned on the latch run #1 left, so
 * exactly ONE network request landed nowhere and the transcript never rendered.
 *
 * The fix keeps `hydratedSessionRef` as a SUCCESS / permanent-rejection latch
 * only (never set at dispatch), so a superseded in-flight run cannot block the
 * next run from fetching; the per-session attempt ledger still bounds retries.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));
vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => <div data-testid="account-stub" /> }));
vi.mock("@/components/ChatHistoryDrawer", () => ({ ChatHistoryDrawer: () => <div data-testid="history-stub" /> }));
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: () => <div data-testid="lead-form-stub" />,
}));
vi.mock("@/features/auth/queryAuthToken", () => ({
  firebaseQueryAuthToken: vi.fn().mockResolvedValue("idp_cust_tok"),
}));

const fetchChatSessionMessagesMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({
  QueryRequestError: class QueryRequestError extends Error {},
  QueryStreamError: class QueryStreamError extends Error {},
  streamQuery: vi.fn(),
  fetchGreeting: vi.fn(),
  fetchChatSessionMessages: fetchChatSessionMessagesMock,
  fetchTrainingSession: vi.fn(),
  fetchTrainingSessions: vi.fn(),
  setQueryAuthTokenProvider: vi.fn(),
}));
vi.mock("@/lib/projectCatalog", () => ({
  fetchProjectCatalog: vi.fn().mockResolvedValue([]),
}));

import { ChatPage } from "@/components/ChatPage";

const TRANSCRIPT = [
  { role: "user" as const, content: "DEEPLINK_USER_TURN" },
  { role: "assistant" as const, content: "DEEPLINK_ASSISTANT_TURN" },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("ChatPage customer deep-link hydration re-run", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    // Greeting latch set as the harness requires so no first-visit greeting
    // competes with the deep-link transcript.
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    window.history.replaceState(null, "", "/sales/chat?sessionId=sess-deep");
    fetchChatSessionMessagesMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders the transcript exactly once under StrictMode double-mount", async () => {
    fetchChatSessionMessagesMock.mockResolvedValue(TRANSCRIPT);

    render(
      <React.StrictMode>
        <AntdApp>
          <ChatPage mode="customer" sessionId="sess-deep" />
        </AntdApp>
      </React.StrictMode>
    );

    // The transcript lands (proving the cancelled first run did not strand it).
    await waitFor(() => expect(screen.getByText("DEEPLINK_ASSISTANT_TURN")).toBeTruthy());
    // Rendered exactly once — no duplicate bubbles from the two dispatches.
    expect(screen.getAllByText("DEEPLINK_USER_TURN")).toHaveLength(1);
    expect(screen.getAllByText("DEEPLINK_ASSISTANT_TURN")).toHaveLength(1);
  });

  it("lands from the surviving run when a dep churn cancels the first fetch mid-flight", async () => {
    // StrictMode's cleanup is the in-flight cancellation a real dep change
    // causes; deferred promises let us resolve the cancelled run and the
    // surviving run independently to prove neither is lost nor duplicated.
    const first = deferred<typeof TRANSCRIPT>();
    const second = deferred<typeof TRANSCRIPT>();
    fetchChatSessionMessagesMock
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    render(
      <React.StrictMode>
        <AntdApp>
          <ChatPage mode="customer" sessionId="sess-deep" />
        </AntdApp>
      </React.StrictMode>
    );

    // Two dispatches: the superseded run #1 and the fresh run #2 (the fix lets
    // run #2 fetch instead of early-returning on run #1's stale dispatch latch).
    await waitFor(() => expect(fetchChatSessionMessagesMock).toHaveBeenCalledTimes(2));

    // Resolve the CANCELLED run #1 first: it must not render (its cleanup ran).
    first.resolve(TRANSCRIPT);
    await Promise.resolve();
    expect(screen.queryByText("DEEPLINK_ASSISTANT_TURN")).toBeNull();

    // Resolve the surviving run #2: the transcript renders exactly once.
    second.resolve(TRANSCRIPT);
    await waitFor(() => expect(screen.getByText("DEEPLINK_ASSISTANT_TURN")).toBeTruthy());
    expect(screen.getAllByText("DEEPLINK_USER_TURN")).toHaveLength(1);
    expect(screen.getAllByText("DEEPLINK_ASSISTANT_TURN")).toHaveLength(1);
  });
});
