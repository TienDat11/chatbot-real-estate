import { describe, expect, it } from "vitest";
import { resolveRedirectTargetAfterLogin } from "@/features/auth/loginRedirect";

function resolveFor(role: "admin" | "sales" | "viewer" | null, next: string | null = null) {
  return resolveRedirectTargetAfterLogin({ role, requestedRedirectPath: next });
}

describe("resolveRedirectTargetAfterLogin", () => {
  it("sends admin and sales to the requested next path", () => {
    expect(resolveFor("admin", "/admin/leads")).toBe("/admin/leads");
    expect(resolveFor("sales", "/admin/leads")).toBe("/admin/leads");
  });

  it("defaults admin and sales to /admin when no next path was given", () => {
    expect(resolveFor("admin", null)).toBe("/admin");
    expect(resolveFor("sales", null)).toBe("/admin");
  });

  it("always sends viewer to the public chat, ignoring the next path", () => {
    expect(resolveFor("viewer", "/admin")).toBe("/");
    expect(resolveFor("viewer", null)).toBe("/");
  });

  it("treats a missing role claim as least privilege (public chat)", () => {
    expect(resolveFor(null, "/admin")).toBe("/");
  });

  it("rejects open-redirect attempts through the next parameter", () => {
    expect(resolveFor("admin", "https://evil.example.com")).toBe("/admin");
    expect(resolveFor("admin", "//evil.example.com")).toBe("/admin");
    expect(resolveFor("admin", "https:/evil.example.com")).toBe("/admin");
    expect(resolveFor("admin", "\\\\evil.example.com")).toBe("/admin");
  });

  it("keeps deep internal paths with query strings", () => {
    expect(resolveFor("admin", "/admin/leads?project=soleil")).toBe("/admin/leads?project=soleil");
  });
});
