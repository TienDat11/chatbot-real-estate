// @vitest-environment jsdom
/**
 * Training deep-link hydration adopts the session's own project (QC fix).
 *
 * /sales/train passes only ?sessionId, so the local training project starts at
 * the default fallback. A valid deep link to a session created under a
 * DIFFERENT project (e.g. soleil) must ADOPT that project as the active scope
 * (transcript lands in the right bucket, the project Select shows it, URL kept)
 * instead of being rejected against the default. The fetch is owner-gated by
 * the bearer, so trusting the returned context is safe.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  firebaseQueryAuthToken: vi.fn().mockResolvedValue("idp_train_tok"),
}));

const fetchTrainingSessionMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({
  QueryRequestError: class QueryRequestError extends Error {},
  QueryStreamError: class QueryStreamError extends Error {},
  streamQuery: vi.fn(),
  fetchGreeting: vi.fn(),
  fetchChatSessionMessages: vi.fn(),
  fetchTrainingSession: fetchTrainingSessionMock,
  fetchTrainingSessions: vi.fn(),
  setQueryAuthTokenProvider: vi.fn(),
}));
vi.mock("@/lib/projectCatalog", () => ({
  fetchProjectCatalog: vi.fn().mockResolvedValue([]),
}));

import { ChatPage } from "@/components/ChatPage";

describe("ChatPage training deep-link project adoption", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    window.history.replaceState(null, "", "/sales/train?sessionId=sess-soleil");
    fetchTrainingSessionMock.mockReset();
    vi.mocked(fetchTrainingSessionMock).mockResolvedValue({
      id: "sess-soleil",
      title: null,
      context: { project_key: "soleil" },
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      message_count: 2,
      answer_mode: "training",
      messages: [
        { role: "user", content: "CHUOI_USER_SOIEIL", created_at: "2026-01-01T00:00:00Z" },
        { role: "assistant", content: "CHUOI_ASSISTANT_SOIEIL", created_at: "2026-01-01T00:00:01Z" },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("adopts the session's project when the route supplies none, landing the transcript in that bucket", async () => {
    render(
      <AntdApp>
        <ChatPage mode="training" sessionId="sess-soleil" />
      </AntdApp>
    );

    // Transcript renders (proves it landed under the now-active soleil scope,
    // not the default camellia bucket which only holds the intro).
    await waitFor(() => expect(screen.getByText("CHUOI_ASSISTANT_SOIEIL")).toBeTruthy());
    expect(screen.getByText("CHUOI_USER_SOIEIL")).toBeTruthy();
    // The project Select reflects the adopted project.
    expect(screen.getByText("The Soleil Đà Nẵng")).toBeTruthy();
    // Deep link is preserved (not cleared) since adoption succeeded.
    expect(window.location.search).toContain("sessionId=sess-soleil");
    // Fetched exactly once: the projectKey-driven re-run is latched, no loop.
    await waitFor(() => expect(fetchTrainingSessionMock).toHaveBeenCalledTimes(1));
  });
});
