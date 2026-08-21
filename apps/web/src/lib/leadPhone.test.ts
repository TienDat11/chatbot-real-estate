import { describe, expect, it } from "vitest";
import { normalizePhone, PHONE_PATTERN } from "@/components/LeadForm";

// Story 5.7 phone handling: separators are stripped before matching so the
// senior-first inputs tolerate spaces, dots, commas and hyphens exactly like
// the backend normalize_phone (api/application/services/lead_service.py).
describe("normalizePhone", () => {
  it("strips spaces, dots, commas and hyphens", () => {
    expect(normalizePhone("0905 123 456")).toBe("0905123456");
    expect(normalizePhone("0905.123.456")).toBe("0905123456");
    expect(normalizePhone("0905-123-456")).toBe("0905123456");
    expect(normalizePhone("0905,123,456")).toBe("0905123456");
  });

  it("strips a mix of separators and surrounding whitespace", () => {
    expect(normalizePhone(" +84 905-123.456 ")).toBe("+84905123456");
    expect(normalizePhone("  0905123456  ")).toBe("0905123456");
  });

  it("keeps the plus sign for the +84 form", () => {
    expect(normalizePhone("+84 905 123 456")).toBe("+84905123456");
  });

  it("returns an empty string when only separators are present", () => {
    expect(normalizePhone(" -., ")).toBe("");
    expect(normalizePhone("")).toBe("");
  });
});

describe("PHONE_PATTERN", () => {
  it.each([
    "0905123456",
    "0987654321",
    "0321234567",
    "0555123456",
    "0777123456",
    "0888123456",
    "0999123456",
    "+84905123456",
  ])("accepts a valid Vietnamese mobile number: %s", (phone) => {
    expect(PHONE_PATTERN.test(normalizePhone(phone))).toBe(true);
  });

  it.each([
    "123",
    "123456789",
    "090512345",
    "09051234567",
    "0123456789",
    "0234567890",
    "0495123456",
    "+8490512345",
    "abcdefghij",
  ])("rejects an invalid phone: %s", (phone) => {
    expect(PHONE_PATTERN.test(normalizePhone(phone))).toBe(false);
  });

  it("rejects when separators are present without normalization", () => {
    expect(PHONE_PATTERN.test("0905 123 456")).toBe(false);
  });
});
