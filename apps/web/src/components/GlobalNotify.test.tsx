// @vitest-environment jsdom
import type { ReactNode } from "react";
import { useEffect } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SalesNotificationProvider, useSalesNotifications } from "@/features/notifications/SalesNotificationProvider";
import { AppShell } from "@/components/AppShell";

const authState = vi.hoisted(() => ({ user: { uid: "sales-1", role: "sales" as "sales" | "admin" | "customer", displayName: "Lan" }, loading: false }));
const streams: Array<{ options: { onIncomingLead?: (lead: Lead) => void } }> = [];
const { notificationOpen, browserNotify } = vi.hoisted(() => ({ notificationOpen: vi.fn(), browserNotify: vi.fn() }));

type Lead = { id: string; leadId: string; projectKey: string; maskedPhone: string | null; name: string; createdAt: string };

vi.mock("@/lib/AuthProvider", () => ({ useAuth: () => authState, useOptionalAuth: () => authState }));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({ getFreshIdToken: vi.fn(async () => "token") }));
vi.mock("@/features/crm/useCrmLeadStream", () => ({
  CrmLeadStreamProvider: ({ options, children }: { options: { onIncomingLead?: (lead: Lead) => void }; children: ReactNode }) => {
    useEffect(() => { streams.push({ options }); }, []);
    return children;
  },
}));
vi.mock("@/features/crm/browserLeadNotifier", () => ({ showBrowserLeadNotification: browserNotify }));
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return { ...actual, App: { useApp: () => ({ notification: { open: notificationOpen } }) } };
});
vi.mock("@/features/notifications/NotificationCenter", () => ({
  NotificationCenter: () => {
    const state = useSalesNotifications();
    return state ? <div data-testid="notification-center" aria-label={`${state.unreadCount} unread`} /> : null;
  },
}));
vi.mock("next/link", () => ({ default: ({ href, children, ...props }: { href: string; children: ReactNode }) => <a href={href} {...props}>{children}</a> }));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => <button type="button">Tài khoản</button> }));

// leadId mirrors Postgres leads.id 1:1 per doc (server read model keys notifications by it).
function lead(id = "lead-1", leadId = "42"): Lead { return { id, leadId, projectKey: "camellia", maskedPhone: null, name: "Nguyễn An", createdAt: "2026-01-01T00:00:00Z" }; }
function response(body: unknown) { return { ok: true, json: async () => body } as Response; }
function Surface() {
  const state = useSalesNotifications();
  return <>{state && <><div role="status" aria-live="polite">{state.unreadCount ? `${state.unreadCount} thông báo chưa đọc` : ""}</div><span data-testid="badge">{state.unreadCount}</span>{state.items.map((item) => <span key={item.id}><a href={`/sales/leads?lead=${item.lead_id}`}>{item.display_name}</a><button onClick={() => void state.markRead(item.id)}>Đã đọc</button></span>)}</>}</>;
}

beforeEach(() => { vi.stubGlobal("fetch", vi.fn(async () => response({ items: [], unread_count: 0 }))); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); streams.length = 0; notificationOpen.mockClear(); browserNotify.mockClear(); authState.user = { uid: "sales-1", role: "sales", displayName: "Lan" }; });

describe("GLOBAL-NOTIFY persistent sales shell", () => {
  it("keeps one listener and surfaces post-snapshot assignments across sales routes", async () => {
    const view = render(<SalesNotificationProvider><Surface /><div data-route="/sales/leads" /></SalesNotificationProvider>);
    await waitFor(() => expect(streams).toHaveLength(1));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    for (const [index, route] of ["/sales/leads", "/sales/chat", "/sales/train"].entries()) {
      view.rerender(<SalesNotificationProvider><Surface /><div data-route={route} /></SalesNotificationProvider>);
      act(() => streams[0].options.onIncomingLead?.(lead(`lead-${index + 1}`, String(index + 41))));

      expect(streams).toHaveLength(1);
      expect(notificationOpen).toHaveBeenCalledTimes(index + 1);
      expect(notificationOpen).toHaveBeenLastCalledWith(expect.objectContaining({ message: "Lead mới" }));
      expect(browserNotify).toHaveBeenCalledTimes(index + 1);
      expect(screen.getByTestId("badge").textContent).toBe(String(index + 1));
      expect(screen.getByRole("status").textContent).toContain(`${index + 1} thông báo chưa đọc`);
      expect(screen.getAllByRole("link", { name: "Nguyễn An" }).at(-1)?.getAttribute("href")).toBe(`/sales/leads?lead=${index + 41}`);
    }
  });

  it("keeps one unread notification for a duplicate reconnect assignment", async () => {
    render(<SalesNotificationProvider><Surface /></SalesNotificationProvider>);
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => { streams[0].options.onIncomingLead?.(lead()); streams[0].options.onIncomingLead?.(lead()); });
    expect(screen.getByTestId("badge").textContent).toBe("1");
  });

  it("dedupes reconnect arrivals, makes read idempotent, and clears on UID/logout", async () => {
    const fetchMock = vi.mocked(fetch);
    const view = render(<SalesNotificationProvider><Surface /></SalesNotificationProvider>);
    await waitFor(() => expect(streams).toHaveLength(1));
    act(() => { streams[0].options.onIncomingLead?.(lead()); streams[0].options.onIncomingLead?.(lead()); });
    expect(screen.getByTestId("badge").textContent).toBe("1");
    authState.user = { uid: "sales-2", role: "sales", displayName: "Mai" };
    view.rerender(<SalesNotificationProvider><Surface /></SalesNotificationProvider>);
    expect(screen.getByTestId("badge").textContent).toBe("0");
    authState.user = { uid: "", role: "sales", displayName: "" };
    view.rerender(<SalesNotificationProvider><Surface /></SalesNotificationProvider>);
    await waitFor(() => expect(screen.queryByRole("link", { name: "Nguyễn An" })).toBeNull());
    expect(fetchMock).toBeDefined();
  });

  it("marks a notification read once and removes it from the unread badge", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "lead-1", lead_id: 42, project_key: "camellia", display_name: "Nguyễn An", created_at: "2026-01-01", read_at: null }], unread_count: 1 }))
      .mockResolvedValue(response({})));
    render(<SalesNotificationProvider><Surface /></SalesNotificationProvider>);
    await waitFor(() => expect(screen.getByTestId("badge").textContent).toBe("1"));
    await act(async () => { screen.getByRole("button", { name: "Đã đọc" }).click(); screen.getByRole("button", { name: "Đã đọc" }).click(); });
    await waitFor(() => expect(screen.getByTestId("badge").textContent).toBe("0"));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("does not expose or alert non-sales users", async () => {
    for (const role of ["admin", "customer"] as const) {
      authState.user = { uid: `${role}-1`, role, displayName: role };
      render(<SalesNotificationProvider><AppShell title="Workspace"><Surface /></AppShell></SalesNotificationProvider>);
      await waitFor(() => expect(streams).toHaveLength(1));
      act(() => streams[0].options.onIncomingLead?.(lead(`${role}-lead`)));
      expect(screen.queryByTestId("notification-center")).toBeNull();
      expect(notificationOpen).not.toHaveBeenCalled();
      expect(browserNotify).not.toHaveBeenCalled();
      cleanup();
      streams.length = 0;
    }
  });
});
