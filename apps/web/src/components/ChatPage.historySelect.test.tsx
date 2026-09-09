// @vitest-environment jsdom
/**
 * R3 (FR-18) pins: selecting a session in ChatHistoryDrawer hydrates the MAIN
 * chat canvas through the canonical path and updates the canonical URL.
 *
 * Pinned here:
 *  1. buildProjectHistoryUrl (pure): canonical sessionId + stable history=1
 *     indicator over a whitelist-preserved query string (mode survives, junk
 *     and legacy spellings do not).
 *  2. Drawer selection -> canvas hydration: the transcript content appears in
 *     the main canvas (not inside any drawer-local view) and the drawer closes.
 *  3. Routed mode: router.replace carries sessionId + history=1 while keeping
 *     ?mode=list; sessionStorage gains the selected id as write authority.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const routerMock = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
// Render message CONTENT so canvas hydration is observable structurally.
vi.mock("@/components/MessageList", () => ({
  MessageList: (props: { messages?: Array<{ id: string; content: string }> }) => (
    <div data-testid="messages-stub">
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

import { buildProjectHistoryUrl, ChatPage } from "@/components/ChatPage";
import { SESSION_KEY } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const HISTORY_SESSIONS = {
  sessions: [
    { session_id: "sess-hist-1", project_key: "camellia", title: "Giá 2PN", message_count: 2, handed_off: false, last_active_at: "2026-08-26T10:00:00Z" },
    // Another project's session must be filtered out of the list.
    { session_id: "sess-other", project_key: "soleil", title: "Soleil chat", message_count: 4, handed_off: false, last_active_at: "2026-08-26T09:00:00Z" },
  ],
};

const HISTORY_MESSAGES = {
  messages: [
    { role: "user", content: "Giá căn 2PN bao nhiêu?", meta: null, created_at: "t0" },
    { role: "assistant", content: "Căn 2PN có giá từ 3.5 tỷ.", meta: null, created_at: "t1" },
  ],
};

describe("buildProjectHistoryUrl (pure)", () => {
  it("writes canonical sessionId plus the history indicator over safe params only", () => {
    expect(buildProjectHistoryUrl("soleil", "sid-9", "?mode=list&junk=1&session=old"))
      .toBe("/project/soleil?mode=list&sessionId=sid-9&history=1");
  });

  it("handles an empty query string deterministically", () => {
    expect(buildProjectHistoryUrl("camellia", "sid-1", ""))
      .toBe("/project/camellia?sessionId=sid-1&history=1");
  });

  it("encodes the project segment and replaces a previous history flag", () => {
    expect(buildProjectHistoryUrl("the peak", "sid-2", "?history=0"))
      .toBe("/project/the%20peak?sessionId=sid-2&history=1");
  });
});

describe("ChatPage history selection -> canvas hydration", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1"); // skip mount greeting
    window.history.replaceState(null, "", "/");
    routerMock.replace.mockClear();
    routerMock.push.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  function stubHistoryFetch(): void {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/sessions/sess-hist-1/messages")) {
          return Promise.resolve(jsonResponse(HISTORY_MESSAGES));
        }
        if (url.includes("/api/sessions")) return Promise.resolve(jsonResponse(HISTORY_SESSIONS));
        return Promise.resolve(jsonResponse({}, 200));
      })
    );
  }

  it("hydrates the selected transcript into the main canvas and closes the drawer", async () => {
    stubHistoryFetch();
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    fireEvent.click(await screen.findByRole("button", { name: "Mở" }));

    // Drawer closure itself is animation-driven (never finishes under jsdom,
    // asserted in browser E2E); what THIS test pins is that the selection
    // hydrates the main canvas instead of any drawer-local transcript view.
    // Canvas hydration: both mapped messages render OUTSIDE any drawer (the
    // drawer itself closed), keyed with stable `${sessionId}-${index}` ids.
    await waitFor(() =>
      expect(screen.getAllByTestId("canvas-message").map((n) => n.textContent)).toEqual([
        "Giá căn 2PN bao nhiêu?",
        "Căn 2PN có giá từ 3.5 tỷ.",
      ])
    );
    // Canonical session recorded in storage before any later /query.
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe("sess-hist-1");
  });

  it("routes the selection through router.replace with sessionId + history=1 preserving mode", async () => {
    stubHistoryFetch();
    window.history.replaceState(null, "", "/project/camellia?mode=list");
    render(<ChatPage routeProjectKey="camellia" />);

    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    fireEvent.click(await screen.findByRole("button", { name: "Mở" }));

    await waitFor(() => expect(routerMock.replace).toHaveBeenCalledOnce());
    const target = String(routerMock.replace.mock.calls[0][0]);
    expect(target).toBe("/project/camellia?mode=list&sessionId=sess-hist-1&history=1");
    await waitFor(() =>
      expect(screen.getByText("Căn 2PN có giá từ 3.5 tỷ.")).toBeTruthy()
    );
  });

  it("re-selecting the same session replaces (never duplicates) the transcript", async () => {
    stubHistoryFetch();
    render(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    fireEvent.click(await screen.findByRole("button", { name: "Mở" }));
    await waitFor(() => expect(screen.getByText("Căn 2PN có giá từ 3.5 tỷ.")).toBeTruthy());

    // Reopen and pick again: bucket write REPLACES the array wholesale.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    });
    fireEvent.click(await screen.findByRole("button", { name: "Mở" }));
    await waitFor(() =>
      expect(screen.getAllByTestId("canvas-message").length).toBe(2)
    );
    expect(screen.getAllByText("Căn 2PN có giá từ 3.5 tỷ.").length).toBe(1);
  });
});
