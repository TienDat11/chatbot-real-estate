// @vitest-environment jsdom
/**
 * R7 (FR-22, G3-r4) pins: on /project/* the URL segment is the SOLE project
 * authority. After a picker click AND after deep-link hydration these must
 * converge: rendered project == storage bucket == active project == router
 * path == request project_key. A stale effect must not mint/restore an old
 * project (the screenshot regression: /project/camellia showing Soleil).
 *
 * Mock notes:
 *  - next/navigation useRouter is structural; navigation completion is
 *    simulated by pushState-ing the pushed canonical URL + rerendering with
 *    the delivered route segment (mirrors what Next does across the commit).
 *  - The history endpoint honors the server contract enforced by
 *    api/interfaces/api/sessions.py: the transcript resolves ONLY when the
 *    request's project_key owns that session (otherwise 404).
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

let mapProjectName = "";
vi.mock("@/components/MapPanel", () => ({
  MapPanel: (props: { project?: { name?: string } }) => {
    mapProjectName = props.project?.name ?? "";
    return <div data-testid="map-stub" />;
  },
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));

const renderedBucketTexts = vi.hoisted(() => ({ current: [] as string[] }));
vi.mock("@/components/MessageList", () => ({
  MessageList: (props: { messages?: Array<{ content?: string }> }) => {
    renderedBucketTexts.current = (props.messages ?? []).map((m) => m.content ?? "");
    return (
      <div data-testid="messages-stub">
        {(props.messages ?? []).map((m, i) => (
          <p key={i}>{m.content}</p>
        ))}
      </div>
    );
  },
}));
vi.mock("@/components/Composer", () => ({
  Composer: (props: { disabled?: boolean }) => (
    <input aria-label="Câu hỏi" disabled={props.disabled} readOnly />
  ),
}));
let leadFormOpen = false;
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: (props: { open: boolean }) => {
    leadFormOpen = props.open;
    return props.open ? <div data-testid="lead-form-stub" /> : null;
  },
}));
vi.mock("@/features/chat/ProjectPicker", () => ({
  ProjectPicker: (props: { onSelect: (key: string) => void }) => (
    <div data-testid="picker-stub">
      <button type="button" onClick={() => props.onSelect("camellia")}>pick-camellia</button>
      <button type="button" onClick={() => props.onSelect("soleil")}>pick-soleil</button>
    </div>
  ),
}));
vi.mock("@/components/ChatHistoryDrawer", () => ({
  ChatHistoryDrawer: () => <div data-testid="history-drawer-stub" />,
}));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => null }));
vi.mock("@/components/AccessibilityControls", () => ({
  AccessibilityControls: () => null,
}));

import { ANON_TOKEN_KEY, DEVICE_ID_KEY, PROJECT_KEY_STORAGE } from "@/features/chat/identity";
import { ChatPage } from "@/components/ChatPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface TestFetch {
  helloBodies: Array<{ project_key?: string; audience?: string }>;
  messageUrls: string[];
}

/** Installs a fetch double covering projects/hello/sessions/anon-token. */
function installFetch(): TestFetch {
  const recorded: TestFetch = { helloBodies: [], messageUrls: [] };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/projects")) {
      return Promise.resolve(jsonResponse({ projects: [
        { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
        { project_key: "soleil", name: "The Soleil Đ Châu Á - Đà Nẵng", is_hot: true, lat: 16.0509, lng: 108.2418 },
      ] }));
    }
    if (url.includes("/api/llms-hello")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as { project_key?: string; audience?: string };
      recorded.helloBodies.push(body);
      return Promise.resolve(jsonResponse({
        greeting: body.project_key === "soleil" ? "HELLO_SOLEIL" : "HELLO_CAMELLIA",
        suggestions: [],
      }));
    }
    if (url.includes("/api/anon/token")) {
      return Promise.resolve(jsonResponse({ anon_token: "tok_route_test" }));
    }
    if (url.includes("/api/sessions/") && url.includes("/messages")) {
      recorded.messageUrls.push(url);
      // Server contract (sessions.py): session lookup scoped by project_key.
      if (url.includes("/api/sessions/sess_cam_ok/messages") && url.includes("project_key=camellia")) {
        return Promise.resolve(jsonResponse({
          session_id: "sess_cam_ok",
          messages: [
            { role: "user", content: "MARKER_CAMELLIA_QUESTION", meta: {}, created_at: "2026-08-27T00:00:00Z" },
            { role: "assistant", content: "MARKER_CAMELLIA_ANSWER", meta: {}, created_at: "2026-08-27T00:00:01Z" },
          ],
        }));
      }
      return Promise.resolve(jsonResponse({ detail: "Session not found" }, 404));
    }
    return Promise.resolve(jsonResponse({}));
  }));
  return recorded;
}

type RenderRouteOptions = { projectKey: string; sessionId?: string };

function renderRouted({ projectKey, sessionId }: RenderRouteOptions) {
  window.history.pushState(
    {},
    "",
    `/project/${projectKey}${sessionId ? `?sessionId=${sessionId}` : ""}`
  );
  const utils = render(<ChatPage routeProjectKey={projectKey} sessionId={sessionId} />);
  return {
    ...utils,
    deliverNavigation(segment: string, sessionIdParam?: string) {
      // Next finished the client transition: the address bar now shows the
      // pushed canonical URL and the page prop carries the new segment.
      window.history.pushState(
        {},
        "",
        `/project/${segment}${sessionIdParam ? `?sessionId=${sessionIdParam}` : ""}`
      );
      utils.rerender(<ChatPage routeProjectKey={segment} sessionId={sessionIdParam} />);
    },
  };
}

describe("FR-22 deep-link hydration scoping", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    window.localStorage.setItem(DEVICE_ID_KEY, "dev_route_1");
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_route_1");
    authMock.role = undefined;
    leadFormOpen = false;
    renderedBucketTexts.current = [];
    mapProjectName = "";
    routerMock.push.mockClear();
    routerMock.replace.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    window.history.pushState({}, "", "/");
  });

  it("a cross-project deep link (/project/camellia?sessionId=<Soleil>) requests camellia scope and renders NOTHING foreign", async () => {
    // Restore-path bait: storage claims soleil; the route says camellia and
    // MUST win without overriding or restoring.
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "soleil");
    const recorded = installFetch();

    renderRouted({ projectKey: "camellia", sessionId: "sess_soleil_foreign" });
    await waitFor(() => expect(recorded.messageUrls.length).toBeGreaterThan(0));

    // Request project_key bound to the ROUTE segment (not mutable state/storage).
    for (const url of recorded.messageUrls) {
      expect(url).toContain(`project_key=camellia`);
      expect(url).toContain(`/api/sessions/sess_soleil_foreign/messages`);
    }
    // No soleil transcript ever rendered into any bucket/canvas.
    expect(renderedBucketTexts.current.join("|")).not.toContain("SOLEIL");
    // Map follows the routed project — storage did not override the route.
    expect(mapProjectName).toBe("The Camellia Sơn Trà - Đà Nẵng");
    // Ownership rejection degrades to a normal guest chat (composer enabled,
    // no wall, no crash); URL stays on the route with a canonical sessionId.
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);
    expect(window.location.pathname).toBe("/project/camellia");
    expect(new URLSearchParams(window.location.search).get("sessionId")).toBe("sess_soleil_foreign");
    expect(new URLSearchParams(window.location.search).has("session")).toBe(false);
  });

  it("a SAME-project deep link hydrates its transcript into the route-scoped bucket", async () => {
    const recorded = installFetch();
    renderRouted({ projectKey: "camellia", sessionId: "sess_cam_ok" });
    await screen.findByText("MARKER_CAMELLIA_ANSWER");

    expect(recorded.messageUrls[0]).toContain("project_key=camellia");
    expect(screen.getByText("MARKER_CAMELLIA_QUESTION")).toBeTruthy();
    // Canonical write: sessionId param present, legacy spelling never written.
    expect(new URLSearchParams(window.location.search).get("sessionId")).toBe("sess_cam_ok");
    expect(new URLSearchParams(window.location.search).has("session")).toBe(false);
  });
});

describe("FR-22 picker-switch convergence (click + rapid switch)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    window.localStorage.setItem(DEVICE_ID_KEY, "dev_route_2");
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_route_2");
    authMock.role = undefined;
    leadFormOpen = false;
    renderedBucketTexts.current = [];
    mapProjectName = "";
    routerMock.push.mockClear();
    routerMock.replace.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    window.history.pushState({}, "", "/");
  });

  function simulateNavigationDelivery(): string | null {
    const pushUrl = routerMock.push.mock.calls.at(-1)?.[0];
    if (typeof pushUrl !== "string") return null;
    window.history.pushState({}, "", pushUrl);
    const match = /\/project\/([^/?]+)/.exec(pushUrl);
    utils.rerender(
      <ChatPage routeProjectKey={match ? decodeURIComponent(match[1]) : undefined} />
    );
    return pushUrl;
  }

  let utils: ReturnType<typeof renderRouted>;

  it("picker click converges route/bucket/storage/path/request; delivery of the optimistic push is NOT re-applied", async () => {
    const recorded = installFetch();
    utils = renderRouted({ projectKey: "camellia" });
    await waitFor(() => expect(mapProjectName).toBe("The Camellia Sơn Trà - Đà Nẵng"));

    fireEvent.click(screen.getByRole("button", { name: "pick-soleil" }));

    // Optimistic application is complete BEFORE Next finishes navigating.
    await waitFor(() =>
      expect(recorded.helloBodies.some((b) => b.project_key === "soleil")).toBe(true)
    );
    expect(window.localStorage.getItem(PROJECT_KEY_STORAGE)).toBe("soleil");
    expect(mapProjectName).toBe("The Soleil Đ Châu Á - Đà Nẵng");
    expect(await screen.findByText("HELLO_SOLEIL")).toBeTruthy();

    // Navigation delivers the pushed canonical path; the route-sync effect
    // must SKIP this self-inflicted move (no duplicate greeting/mint/divergent
    // session id between the pushed URL and the applied context).
    const pushedUrl = simulateNavigationDelivery();
    expect(pushedUrl).toMatch(/^\/project\/soleil\?sessionId=[0-9a-f-]{36}$/);
    if (pushedUrl === null) throw new Error("unreachable after toMatch");
    expect(window.location.pathname).toBe("/project/soleil");
    expect(window.location.search).toBe(new URL(pushedUrl, "http://x").search);
    const soleiHellos = recorded.helloBodies.filter((b) => b.project_key === "soleil").length;
    expect(soleiHellos).toBe(1);
    // Bucket, storage and rendered context still agree with the URL.
    expect(screen.getByText("HELLO_SOLEIL")).toBeTruthy();
    expect(screen.queryByText("HELLO_CAMELLIA")).toBeNull();
    expect(window.localStorage.getItem(PROJECT_KEY_STORAGE)).toBe("soleil");
  });

  it("rapid double activation of one target fires exactly ONE greeting and mints ONE session id", async () => {
    const recorded = installFetch();
    utils = renderRouted({ projectKey: "camellia" });
    await waitFor(() => expect(mapProjectName).toBe("The Camellia Sơn Trà - Đà Nẵng"));

    fireEvent.click(screen.getByRole("button", { name: "pick-soleil" }));
    fireEvent.click(screen.getByRole("button", { name: "pick-soleil" }));

    await waitFor(() =>
      expect(recorded.helloBodies.filter((b) => b.project_key === "soleil").length).toBeGreaterThan(0)
    );
    // Allow everything in flight to settle, then pin the dedupe.
    await new Promise((resolve) => setTimeout(resolve, 25));
    expect(recorded.helloBodies.filter((b) => b.project_key === "soleil")).toHaveLength(1);
    expect(routerMock.push).toHaveBeenCalledTimes(1);
  });

  it("external back/forward moves still switch (route-vs-self distinction)", async () => {
    const recorded = installFetch();
    utils = renderRouted({ projectKey: "camellia" });
    await waitFor(() => expect(mapProjectName).toBe("The Camellia Sơn Trà - Đà Nẵng"));

    // Pure external move: no picker click preceded it.
    utils.deliverNavigation("soleil");
    await waitFor(() =>
      expect(recorded.helloBodies.some((b) => b.project_key === "soleil")).toBe(true)
    );
    expect(window.localStorage.getItem(PROJECT_KEY_STORAGE)).toBe("soleil");

    utils.deliverNavigation("camellia");
    await waitFor(() =>
      expect(recorded.helloBodies.some((b) => b.project_key === "camellia")).toBe(true)
    );
    expect(window.localStorage.getItem(PROJECT_KEY_STORAGE)).toBe("camellia");
    expect(mapProjectName).toBe("The Camellia Sơn Trà - Đà Nẵng");
  });
});
