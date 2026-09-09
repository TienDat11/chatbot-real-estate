import { describe, expect, it } from "vitest";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

describe("light visual tokens", () => {
  it("keeps shared surfaces bright with navy text and brand action", () => {
    expect(C.bg).toBe("#F7F9FC");
    expect(C.surface).toBe("#FFFFFF");
    expect(C.text).toBe("#142A43");
    expect(C.primary).toBe("#0E2A47");
  });

  it("uses restrained elevation and consistent control radii", () => {
    expect(SHADOW.card).toContain("rgba(20, 42, 67");
    expect(RADIUS.input).toBe(RADIUS.btn);
  });
});
