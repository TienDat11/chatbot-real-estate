// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AppShell } from "@/components/AppShell";
import { ChatPage } from "@/components/ChatPage";

const routerMock = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
}));
const authMock = vi.hoisted(() => ({ role: "sales" as string | undefined }));

vi.mock("next/navigation", () => ({ useRouter: () => routerMock }));
vi.mock("@/lib/AuthProvider", () => ({
  useOptionalAuth: () => (authMock.role ? { user: { role: authMock.role } } : undefined),
}));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => null }));
vi.mock("@/features/notifications/NotificationCenter", () => ({ NotificationCenter: () => null }));
vi.mock("@/components/AccessibilityControls", () => ({ AccessibilityControls: () => null }));
vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({ MessageList: () => <div data-testid="messages-stub" /> }));
vi.mock("@/components/Composer", () => ({ Composer: () => <div data-testid="composer-stub" /> }));
vi.mock("@/components/LeadForm", () => ({ LeadForm: () => null, LEAD_ID_STORAGE_KEY: "ragre.lead_id" }));
vi.mock("@/components/ChatHistoryDrawer", () => ({ ChatHistoryDrawer: () => null }));
vi.mock("@/features/chat/ProjectPicker", () => ({
  ProjectPicker: ({ open, onSelect }: { open: boolean; onSelect: (key: string) => void }) =>
    open ? <button type="button" role="option" onClick={() => onSelect("soleil")}>The Soleil Đà Nẵng</button> : null,
}));

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.sessionStorage.setItem("ragre.hello_shown", "1");
  window.localStorage.setItem("ragre.device_id", "sales-device");
  window.localStorage.setItem("ragre.anon_token", "sales-token");
  window.history.pushState({}, "", "/sales/chat/project/camellia");
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    if (String(input).includes("/api/projects")) {
      return Promise.resolve(jsonResponse({ projects: [
        { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1, lng: 108.2 },
        { project_key: "soleil", name: "The Soleil Đà Nẵng", is_hot: false, lat: 16.0, lng: 108.2 },
      ] }));
    }
    if (String(input).includes("/api/llms-hello")) {
      return Promise.resolve(jsonResponse({ greeting: "Hello", suggestions: [] }));
    }
    return Promise.resolve(jsonResponse({}));
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.history.pushState({}, "", "/");
});

describe("shell-owned sales ChatPage", () => {
  it("visibly exposes one project switcher and routes a selection with a new valid session", async () => {
    render(
      <AppShell>
        <ChatPage mode="sales" audience="sales" shellOwned routeProjectKey="camellia" />
      </AppShell>
    );

    await waitFor(() => expect(screen.getByRole("button", { name: /Đổi dự án/i })).toBeTruthy());
    expect(screen.getAllByRole("banner")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /Đổi dự án/i })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: /Đổi dự án/i }));
    fireEvent.click(screen.getByRole("option", { name: /The Soleil Đà Nẵng/i }));

    await waitFor(() => expect(routerMock.push).toHaveBeenCalled());
    expect(String(routerMock.push.mock.calls.at(-1)?.[0])).toMatch(
      /^\/sales\/chat\/project\/soleil\?sessionId=[0-9a-f-]{36}$/
    );
  });

  it("keeps customer ChatPage behavior on the customer surface", async () => {
    authMock.role = undefined;
    window.history.pushState({}, "", "/project/camellia");
    render(<ChatPage routeProjectKey="camellia" />);

    await waitFor(() => expect(screen.getByRole("button", { name: /Đổi dự án/i })).toBeTruthy());
    expect(screen.getAllByRole("button", { name: /Đổi dự án/i })).toHaveLength(1);
  });
});
