import { describe, expect, it } from "vitest";
import {
  ADMIN_REDIRECT_PATH,
  SALES_REDIRECT_PATH,
  VIEWER_REDIRECT_PATH,
  resolveRedirectTargetAfterLogin,
} from "@/features/auth/loginRedirect";

function resolveFor(role: "admin" | "sales" | "viewer" | null, next: string | null = null) {
  return resolveRedirectTargetAfterLogin({ role, requestedRedirectPath: next });
}

describe("resolveRedirectTargetAfterLogin", () => {
  it("honours a safe requested next path for staff roles", () => {
    expect(resolveFor("admin", "/admin/leads")).toBe("/admin/leads");
    expect(resolveFor("sales", "/train")).toBe("/train");
  });

  it("sends sales to the CRM lead board by default (ISSUE-5 FR-4)", () => {
    expect(SALES_REDIRECT_PATH).toBe("/sales/leads");
    expect(resolveFor("sales", null)).toBe("/sales/leads");
    expect(resolveFor("sales", "")).toBe("/sales/leads");
  });

  it("keeps the admin and viewer defaults on the project home", () => {
    expect(ADMIN_REDIRECT_PATH).toBe("/project/camellia");
    expect(VIEWER_REDIRECT_PATH).toBe("/project/camellia");
    expect(resolveFor("admin", null)).toBe("/project/camellia");
  });

  it("keeps viewers and unknown roles on the shared chat home", () => {
    expect(resolveFor("viewer", "/admin")).toBe("/project/camellia");
    expect(resolveFor(null, "/admin")).toBe("/project/camellia");
    expect(resolveFor(null, null)).toBe("/project/camellia");
  });

  it("rejects open-redirect attempts through the next parameter", () => {
    expect(resolveFor("admin", "https://evil.example.com")).toBe("/project/camellia");
    expect(resolveFor("admin", "//evil.example.com")).toBe("/project/camellia");
    expect(resolveFor("admin", "https:/evil.example.com")).toBe("/project/camellia");
    expect(resolveFor("admin", "\\\\evil.example.com")).toBe("/project/camellia");
  });

  it("rejects a next path that loops back into the login route", () => {
    expect(resolveFor("sales", "/login")).toBe("/sales/leads");
    expect(resolveFor("sales", "/login?next=/sales/leads")).toBe("/sales/leads");
    expect(resolveFor("admin", "/login#top")).toBe(ADMIN_REDIRECT_PATH);
  });

  it("rejects a next path that loops back into the bare-root auth shell", () => {
    // "/" always renders the login/guest screen, so honouring it bounces a
    // just-signed-in user back into auth UI instead of their workspace.
    expect(resolveFor("sales", "/")).toBe("/sales/leads");
    expect(resolveFor("admin", "/")).toBe("/project/camellia");
    expect(resolveFor("admin", "/?project_key=camellia")).toBe("/project/camellia");
    expect(resolveFor("viewer", "/")).toBe("/project/camellia");
  });

  it("keeps deep internal paths with query strings", () => {
    expect(resolveFor("sales", "/sales/leads?project=soleil")).toBe(
      "/sales/leads?project=soleil"
    );
  });
});
