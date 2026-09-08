// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/AppShell";

const authState = vi.hoisted(() => ({
  user: { role: "sales", displayName: "Lan" },
  loading: false,
}));

vi.mock("@/lib/AuthProvider", () => ({
  useOptionalAuth: () => authState,
}));

vi.mock("@/components/AccountControls", () => ({
  AccountControls: () => <button type="button">Tài khoản</button>,
}));

vi.mock("@/features/notifications/NotificationCenter", () => ({
  NotificationCenter: () => <button type="button" aria-label="Thông báo, 1 chưa đọc">Thông báo</button>,
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: { href: string; children: ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

describe("AppShell", () => {
  afterEach(cleanup);

  beforeEach(() => {
    authState.user = { role: "sales", displayName: "Lan" };
  });

  it("provides one shared chrome and preserves the page content area", () => {
    render(
      <AppShell title="CRM">
        <section data-testid="workspace">Workspace</section>
      </AppShell>,
    );

    expect(screen.getByRole("banner")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "CRM" })).toBeTruthy();
    expect(screen.getByTestId("workspace")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Thông báo, 1 chưa đọc" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Tài khoản" })).toBeTruthy();
  });

  it("exposes exactly three primary destinations for sales with working cross links", () => {
    render(<AppShell><section data-testid="workspace">Workspace</section></AppShell>);

    const nav = screen.getByRole("navigation", { name: "Điều hướng chính" });
    const navLinks = within(nav).getAllByRole("link");

    // Three destinations: CRM, one Chat bán hàng entry and the training
    // workspace — each backed by its real route (no dead links).
    expect(navLinks.map((link) => link.getAttribute("href"))).toEqual([
      "/sales/leads",
      "/sales/chat",
      "/sales/train",
    ]);
    const labels = navLinks.map((link) => link.textContent?.trim());
    expect(labels[0]).toMatch(/^(CRM|Khách hàng)$/);
    expect(labels[1]).toBe("Chat bán hàng");
    expect(labels[2]).toBe("Đào tạo");

    // No duplicate "Tư vấn" surface: the training link points at the
    // canonical /sales/train destination only.
    expect(within(nav).queryByRole("link", { name: "Tư vấn" })).toBeNull();

    // CRM→Chat and Chat→CRM cross-navigation via the shared chrome: the nav
    // renders on every route content, so each workspace can reach the other.
    expect(within(nav).getByRole("link", { name: /^(CRM|Khách hàng)$/ }).getAttribute("href")).toBe("/sales/leads");
    expect(within(nav).getByRole("link", { name: "Chat bán hàng" }).getAttribute("href")).toBe("/sales/chat");
    expect(within(nav).getByRole("link", { name: "Đào tạo" }).getAttribute("href")).toBe("/sales/train");

    // One AppShell header only.
    expect(screen.getAllByRole("banner")).toHaveLength(1);
  });

  it("keeps the admin nav on all staff workspaces plus admin", () => {
    authState.user = { role: "admin", displayName: "Quản trị" };
    render(<AppShell><section data-testid="workspace">Admin workspace</section></AppShell>);

    const nav = screen.getByRole("navigation", { name: "Điều hướng chính" });
    const hrefs = within(nav).getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual(["/sales/leads", "/sales/chat", "/sales/train", "/admin"]);
  });

  it.each(["admin", "customer"])("does not expose sales notification chrome to a %s", (role) => {
    authState.user = { role, displayName: "Khách" };
    render(<AppShell><section>Non-sales workspace</section></AppShell>);

    expect(screen.queryByRole("button", { name: /Thông báo/ })).toBeNull();
  });
});
