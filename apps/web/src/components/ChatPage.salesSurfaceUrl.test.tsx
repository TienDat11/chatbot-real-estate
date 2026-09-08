// @vitest-environment jsdom
/**
 * Sales surface URL authority: on /sales/chat/project/* every ChatPage
 * project/session write (picker switch, session sync, history deep link,
 * unknown-key fallback) stays on the canonical sales route, while the customer
 * surface keeps writing /project/*. Mirrors the FR-22 harness from
 * ChatPage.routeAuthority.test.tsx with the sales URL contracts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const routerMock = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
}));
const authMock = vi.hoisted(() => ({ role: undefined as string | undefined }));

vi.mock("next/navigation", () => ({ useRouter: () => routerMock }));
vi.mock("@/lib/AuthProvider", () => ({
  useOptionalAuth: () =>
    authMock.role ? { user: { role: authMock.role } } : undefined,
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: (props: { project?: { name?: string } }) => (
    <div data-testid="map-stub" data-name={props.project?.name ?? ""} />
  ),
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({
  MessageList: (props: { messages?: Array<{ content?: string }> }) => (
    <div data-testid="messages-stub">
      {(props.messages ?? []).map((m, i) => (
        <p key={i}>{m.content}</p>
      ))}
    </div>
  ),
}));
vi.mock("@/components/Composer", () => ({
  Composer: (props: { disabled?: boolean }) => (
    <input aria-label="Câu hỏi" disabled={props.disabled} readOnly />
  ),
}));
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: () => null,
}));
vi.mock("@/features/chat/ProjectPicker", () => ({
  ProjectPicker: (props: { onSelect: (key: string) => void }) => (
    <div data-testid="picker-stub">
      <button type="button" onClick={() => props.onSelect("soleil")}>pick-soleil</button>
    </div>
  ),
}));
let historySelect: ((sessionId: string, transcript: unknown) => void) | null = null;
vi.mock("@/components/ChatHistoryDrawer", () => ({
  ChatHistoryDrawer: (props: { onSelectSession?: (sessionId: string, transcript: unknown) => void }) => {
    historySelect = props.onSelectSession ?? null;
    return <div data-testid="history-drawer-stub" />;
  },
}));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => null }));
vi.mock("@/components/AccessibilityControls", () => ({ AccessibilityControls: () => null }));

import { App as AntApp } from "antd";
import { ANON_TOKEN_KEY, DEVICE_ID_KEY } from "@/features/chat/identity";
import { buildProjectHistoryUrl, ChatPage } from "@/components/ChatPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Installs a fetch double covering projects/hello/sessions/anon-token. */
function installFetch(): { helloBodies: Array<{ project_key?: string }>; messageUrls: string[] } {
  const recorded = { helloBodies: [] as Array<{ project_key?: string }>, messageUrls: [] as string[] };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/projects")) {
      return Promise.resolve(jsonResponse({ projects: [
        { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
        { project_key: "soleil", name: "The Soleil Đ Châu Á - Đà Nẵng", is_hot: true, lat: 16.0509, lng: 108.2418 },
      ] }));
    }
    if (url.includes("/api/llms-hello")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { project_key?: string };
      recorded.helloBodies.push(body);
      return Promise.resolve(jsonResponse({ greeting: `HELLO_${body.project_key?.toUpperCase()}`, suggestions: [] }));
    }
    if (url.includes("/api/anon/token")) {
      return Promise.resolve(jsonResponse({ anon_token: "tok_sales_test" }));
    }
    if (url.includes("/api/sessions/") && url.includes("/messages")) {
      recorded.messageUrls.push(url);
      if (url.includes("/api/sessions/sess_cam_ok/messages") && url.includes("project_key=camellia")) {
        return Promise.resolve(jsonResponse({
          session_id: "sess_cam_ok",
          messages: [
            { role: "user", content: "MARKER_SALES_QUESTION", meta: {}, created_at: "2026-08-28T00:00:00Z" },
            { role: "assistant", content: "MARKER_SALES_ANSWER", meta: {}, created_at: "2026-08-28T00:00:01Z" },
          ],
        }));
      }
      return Promise.resolve(jsonResponse({ detail: "Session not found" }, 404));
    }
    return Promise.resolve(jsonResponse({}));
  }));
  return recorded;
}

const SALES_PROPS = { mode: "sales", audience: "sales", shellOwned: true } as const;

function renderSalesRouted(projectKey: string, sessionId?: string) {
  window.history.pushState(
    {},
    "",
    `/sales/chat/project/${projectKey}${sessionId ? `?sessionId=${sessionId}` : ""}`
  );
  // shellOwned consumes the persistent shell's App context (the /sales/layout
  // AntdApp in production); the harness emulates that host minimally instead of
  // asking the canvas to remount its own providers.
  return render(
    <AntApp>
      <ChatPage routeProjectKey={projectKey} sessionId={sessionId} {...SALES_PROPS} />
    </AntApp>
  );
}

describe("sales surface URL authority", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    window.localStorage.setItem(DEVICE_ID_KEY, "dev_sales_1");
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_sales_1");
    authMock.role = undefined;
    historySelect = null;
    routerMock.push.mockClear();
    routerMock.replace.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    window.history.pushState({}, "", "/");
  });

  it("a picker switch pushes the canonical sales chat URL and its delivery is not re-applied", async () => {
    const recorded = installFetch();
    const utils = renderSalesRouted("camellia");
    await waitFor(() =>
      expect(screen.getByTestId("map-stub").getAttribute("data-name")).toBe("The Camellia Sơn Trà - Đà Nẵng")
    );

    fireEvent.click(screen.getByRole("button", { name: "pick-soleil" }));
    await waitFor(() =>
      expect(recorded.helloBodies.some((b) => b.project_key === "soleil")).toBe(true)
    );

    // The write stays on the sales surface — never /project/soleil.
    const pushedUrl = routerMock.push.mock.calls.at(-1)?.[0];
    expect(typeof pushedUrl).toBe("string");
    expect(pushedUrl as string).toMatch(/^\/sales\/chat\/project\/soleil\?sessionId=[0-9a-f-]{36}$/);

    // Deliver the navigation like Next does; the optimistic move must not
    // mint a second greeting/session.
    window.history.pushState({}, "", pushedUrl as string);
    const match = /\/sales\/chat\/project\/([^/?]+)/.exec(pushedUrl as string);
    utils.rerender(
      <AntApp>
        <ChatPage
          routeProjectKey={match ? decodeURIComponent(match[1]) : undefined}
          {...SALES_PROPS}
        />
      </AntApp>
    );
    await new Promise((resolve) => setTimeout(resolve, 25));
    expect(recorded.helloBodies.filter((b) => b.project_key === "soleil")).toHaveLength(1);
  });

  it("a sales deep link hydrates its transcript and keeps the URL on the sales route", async () => {
    const recorded = installFetch();
    renderSalesRouted("camellia", "sess_cam_ok");
    await screen.findByText("MARKER_SALES_ANSWER");

    expect(recorded.messageUrls[0]).toContain("project_key=camellia");
    expect(window.location.pathname).toBe("/sales/chat/project/camellia");
    expect(new URLSearchParams(window.location.search).get("sessionId")).toBe("sess_cam_ok");
    expect(new URLSearchParams(window.location.search).has("session")).toBe(false);
  });

  it("a history selection rewrites the URL on the sales surface with the history marker", async () => {
    const recorded = installFetch();
    window.history.pushState({}, "", "/sales/chat/project/camellia?mode=list");
    render(
      <AntApp>
        <ChatPage routeProjectKey="camellia" {...SALES_PROPS} />
      </AntApp>
    );
    await waitFor(() => expect(historySelect).toBeTruthy());

    historySelect!("sess_cam_ok", [{ role: "user", content: "MARKER_SALES_QUESTION" }]);

    await waitFor(() => expect(routerMock.replace).toHaveBeenCalled());
    expect(String(routerMock.replace.mock.calls.at(-1)?.[0])).toBe(
      "/sales/chat/project/camellia?mode=list&sessionId=sess_cam_ok&history=1"
    );
    expect(window.location.pathname).toBe("/sales/chat/project/camellia");
    expect(new URLSearchParams(window.location.search).get("sessionId")).toBe("sess_cam_ok");
    expect(recorded.messageUrls.length).toBe(0); // hydration comes from the picked transcript, not a fetch
  });

  it("an unknown project key on the sales surface falls back on the sales route", async () => {
    installFetch();
    renderSalesRouted("ghost");

    await waitFor(() => expect(routerMock.replace).toHaveBeenCalled());
    // The fallback keeps the minted session and the sales surface prefix.
    expect(String(routerMock.replace.mock.calls[0][0])).toMatch(
      /^\/sales\/chat\/project\/camellia\?sessionId=[0-9a-f-]{36}$/
    );
  });

  it("buildProjectHistoryUrl keeps the history marker on the requested surface", () => {
    expect(buildProjectHistoryUrl("soleil", "s1", "?mode=list&junk=1", "sales")).toBe(
      "/sales/chat/project/soleil?mode=list&sessionId=s1&history=1"
    );
    expect(buildProjectHistoryUrl("soleil", "s1", "?mode=list&junk=1")).toBe(
      "/project/soleil?mode=list&sessionId=s1&history=1"
    );
  });
});
