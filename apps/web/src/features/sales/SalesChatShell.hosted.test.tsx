// @vitest-environment jsdom
/**
 * SALES-SHELL hosted-stack checkpoint.
 *
 * The sales layout owns ALL chrome: exactly one AppShell header/nav, and the
 * hosted sales chat canvas (SalesChatShell → ChatPage shellOwned) must not add
 * a duplicate staff header, a duplicate main navigation, or a duplicate
 * Ant/theme provider wrapper. The root app/layout.tsx is stood in by the same
 * theme-boundary marker so wrapper duplication is countable; antd's App is
 * wrapped (not replaced) so ChatCanvas still receives the real useApp context.
 */
import type { ReactNode } from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const routerMock = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
}));
const authState = vi.hoisted(() => ({
  user: { role: "sales", displayName: "Lan" },
  loading: false,
}));

vi.mock("next/navigation", () => ({ useRouter: () => routerMock }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: { href: string; children: ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));
vi.mock("@/lib/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  useOptionalAuth: () => authState,
  useAuth: () => authState,
}));
vi.mock("@/components/AccountControls", () => ({
  AccountControls: () => <button type="button">Tài khoản</button>,
}));
vi.mock("@/components/AccessibilityControls", () => ({ AccessibilityControls: () => null }));
vi.mock("@/features/notifications/NotificationCenter", () => ({
  NotificationCenter: () => <button type="button" aria-label="Thông báo">Thông báo</button>,
}));

// Sales layout collaborators render as passthroughs; the layout composition
// itself stays real so the hosted wrapper/header counts are the real ones.
vi.mock("@/components/RequireRole", () => ({
  RequireRole: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/realtime/RealtimeProvider", () => ({
  RealtimeProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/features/notifications/SalesNotificationProvider", () => ({
  SalesNotificationProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

// Theme-boundary marker doubles as the root app/layout.tsx stand-in.
vi.mock("@/components/ProThemeProvider", () => ({
  ProThemeProvider: ({ children }: { children: ReactNode }) => (
    <div data-testid="theme-boundary">{children}</div>
  ),
}));
// antd App keeps working (real useApp context) but emits a countable marker.
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  const RealApp = actual.App;
  const MarkedApp = (({ children }: { children?: ReactNode }) => (
    <div data-testid="ant-app-boundary">
      <RealApp>{children}</RealApp>
    </div>
  )) as unknown as typeof actual.App;
  MarkedApp.useApp = RealApp.useApp;
  return { ...actual, App: MarkedApp };
});

// Shared-canvas heavy children collapse to observable stubs.
vi.mock("@/components/MapPanel", () => ({
  MapPanel: (props: { project?: { name?: string } }) => (
    <div data-testid="map-stub" data-name={props.project?.name ?? ""} />
  ),
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({
  MessageList: () => <div data-testid="messages-stub" />,
}));
vi.mock("@/components/Composer", () => ({
  Composer: () => <input aria-label="Câu hỏi" readOnly />,
}));
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: () => null,
}));
vi.mock("@/features/chat/ProjectPicker", () => ({
  ProjectPicker: () => <div data-testid="picker-stub" />,
}));
vi.mock("@/components/ChatHistoryDrawer", () => ({
  ChatHistoryDrawer: () => <div data-testid="history-drawer-stub" />,
}));

import SalesLayout from "@/app/sales/layout";
import { ChatPage } from "@/components/ChatPage";
import { ProThemeProvider } from "@/components/ProThemeProvider";
import { SalesChatShell } from "@/features/sales/SalesChatShell";
import { ANON_TOKEN_KEY, DEVICE_ID_KEY } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Fetch double covering projects/hello/anon-token (same harness as ChatPage tests). */
function installFetch(): void {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/projects")) {
      return Promise.resolve(jsonResponse({ projects: [
        { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
        { project_key: "soleil", name: "The Soleil Đ Châu Á - Đà Nẵng", is_hot: true, lat: 16.0509, lng: 108.2418 },
      ] }));
    }
    if (url.includes("/api/llms-hello")) {
      return Promise.resolve(jsonResponse({ greeting: "HELLO", suggestions: [] }));
    }
    if (url.includes("/api/anon/token")) {
      return Promise.resolve(jsonResponse({ anon_token: "tok_hosted_test" }));
    }
    return Promise.resolve(jsonResponse({}));
  }));
}

/** Full hosted stack: root theme provider → sales layout (AntdApp + AppShell) → sales chat canvas. */
function renderHosted(projectKey: string, sessionId?: string) {
  window.history.pushState(
    {},
    "",
    `/sales/chat/project/${projectKey}${sessionId ? `?sessionId=${sessionId}` : ""}`
  );
  return render(
    <ProThemeProvider>
      <SalesLayout>
        <SalesChatShell routeProjectKey={projectKey} sessionId={sessionId} />
      </SalesLayout>
    </ProThemeProvider>
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.sessionStorage.setItem("ragre.hello_shown", "1");
  window.localStorage.setItem(DEVICE_ID_KEY, "dev_hosted_1");
  window.localStorage.setItem(ANON_TOKEN_KEY, "tok_hosted_1");
  installFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.history.pushState({}, "", "/");
});

describe("SALES-SHELL hosted stack", () => {
  it("renders one AppShell header, one main nav, and keeps the chat URL canonical", async () => {
    renderHosted("soleil");
    await waitFor(() =>
      expect(screen.getByTestId("map-stub").getAttribute("data-name")).toBe("The Soleil Đ Châu Á - Đà Nẵng")
    );

    // Exactly one chrome header and one main navigation in the whole stack.
    expect(screen.getAllByRole("banner")).toHaveLength(1);
    expect(screen.getAllByRole("navigation", { name: "Điều hướng chính" })).toHaveLength(1);

    // The hosted canvas keeps every URL write on the canonical sales route.
    expect(window.location.pathname).toBe("/sales/chat/project/soleil");
    for (const call of [...routerMock.replace.mock.calls, ...routerMock.push.mock.calls]) {
      expect(String(call[0])).toMatch(/^\/sales\/chat\/project\//);
    }
    // CRM↔Chat cross-navigation stays reachable from the chat route content.
    const nav = screen.getByRole("navigation", { name: "Điều hướng chính" });
    const hrefs = within(nav).getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(hrefs).toContain("/sales/leads");
    expect(hrefs).toContain("/sales/chat");
  });

  it("adds no duplicate Ant/theme wrapper when hosted by the sales shell", async () => {
    renderHosted("soleil");
    await waitFor(() =>
      expect(screen.getByTestId("map-stub").getAttribute("data-name")).toBe("The Soleil Đ Châu Á - Đà Nẵng")
    );

    // One AntApp (the sales layout's) and one theme provider (the root
    // layout's) — the hosted canvas must not stack its own second pair.
    expect(screen.getAllByTestId("ant-app-boundary")).toHaveLength(1);
    expect(screen.getAllByTestId("theme-boundary")).toHaveLength(1);
  });

  it("the standalone customer canvas still provides its own wrapper pair", () => {
    window.history.pushState({}, "", "/project/camellia");
    render(<ChatPage routeProjectKey="camellia" />);

    // Guard for the hosted fix: outside the sales shell the canvas must keep
    // providing AntApp + theme so the customer surface never loses them.
    expect(screen.getAllByTestId("ant-app-boundary")).toHaveLength(1);
    expect(screen.getAllByTestId("theme-boundary")).toHaveLength(1);
  });

  it("the shell-owned canvas contributes no visible staff header or navigation", () => {
    window.history.pushState({}, "", "/sales/chat/project/camellia");
    render(
      <ChatPage
        routeProjectKey="camellia"
        mode="sales"
        audience="sales"
        shellOwned
      />
    );

    // No banner/navigation from the canvas itself; only the shell may own them.
    expect(screen.queryAllByRole("banner")).toHaveLength(0);
    expect(screen.queryAllByRole("navigation", { name: "Điều hướng chính" })).toHaveLength(0);
  });

  it("a non-shell-owned staff canvas still owns its chrome (fix-direction guard)", () => {
    window.history.pushState({}, "", "/sales/chat/project/camellia");
    render(<ChatPage routeProjectKey="camellia" mode="sales" audience="sales" />);

    expect(screen.getAllByRole("banner")).toHaveLength(1);
    expect(screen.getAllByRole("navigation", { name: "Điều hướng chính" })).toHaveLength(1);
  });
});
