import { describe, expect, it } from "vitest";
import { routesForRole } from "@/lib/roleRoutes";

/**
 * SALES-SHELL checkpoint: the shared navigation exposes every real staff
 * workspace — CRM, sales chat and the authenticated training surface. The
 * legacy /train entry survives only as a redirect onto /sales/train; the nav
 * must link the canonical destination so no dead links ship in the shell.
 */
describe("routesForRole", () => {
  it("exposes exactly three sales destinations: CRM, Chat bán hàng and Đào tạo", () => {
    const routes = routesForRole("sales");

    expect(routes.map((route) => route.href)).toEqual([
      "/sales/leads",
      "/sales/chat",
      "/sales/train",
    ]);
    expect(routes.map((route) => route.label)).toEqual([
      expect.stringMatching(/^(CRM|Khách hàng)$/),
      "Chat bán hàng",
      "Đào tạo",
    ]);
    // CRM→Chat cross-navigation via the shared chrome stays intact.
    expect(routes[0]).toMatchObject({ href: "/sales/leads", label: expect.stringMatching(/^(CRM|Khách hàng)$/) });
  });

  it("keeps the admin nav on every staff workspace plus admin", () => {
    const routes = routesForRole("admin");

    expect(routes.map((route) => route.href)).toEqual([
      "/sales/leads",
      "/sales/chat",
      "/sales/train",
      "/admin",
    ]);
  });

  it("does not expose staff navigation to guests or viewers", () => {
    expect(routesForRole(null)).toEqual([]);
    expect(routesForRole("viewer")).toEqual([]);
  });
});
