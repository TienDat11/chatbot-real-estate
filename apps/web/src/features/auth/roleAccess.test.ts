import { describe, expect, it } from "vitest";
import { evaluateRoleAccess, isRoleAllowed } from "@/features/auth/roleAccess";

describe("evaluateRoleAccess", () => {
  it("reports loading while auth state is unresolved, even for an allowed role", () => {
    expect(
      evaluateRoleAccess({ isLoading: true, role: "admin", allowedRoles: ["admin"] })
    ).toBe("loading");
  });

  it("allows a role present in the allow-list", () => {
    expect(evaluateRoleAccess({ isLoading: false, role: "admin", allowedRoles: ["admin"] })).toBe(
      "allowed"
    );
    expect(
      evaluateRoleAccess({ isLoading: false, role: "sales", allowedRoles: ["admin", "sales"] })
    ).toBe("allowed");
  });

  it("denies a role missing from the allow-list", () => {
    expect(evaluateRoleAccess({ isLoading: false, role: "sales", allowedRoles: ["admin"] })).toBe(
      "denied"
    );
    expect(evaluateRoleAccess({ isLoading: false, role: "viewer", allowedRoles: ["admin"] })).toBe(
      "denied"
    );
  });

  it("denies signed-out users and empty allow-lists", () => {
    expect(evaluateRoleAccess({ isLoading: false, role: null, allowedRoles: ["admin"] })).toBe(
      "denied"
    );
    expect(evaluateRoleAccess({ isLoading: false, role: "admin", allowedRoles: [] })).toBe("denied");
  });
});

describe("isRoleAllowed", () => {
  it("is a plain membership check with null never allowed", () => {
    expect(isRoleAllowed("viewer", ["admin", "viewer"])).toBe(true);
    expect(isRoleAllowed(null, ["admin", "viewer"])).toBe(false);
  });
});
