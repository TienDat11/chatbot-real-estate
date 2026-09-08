// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FactEvidence } from "@rag-ragre/contracts";
import {
  FACT_VALUE_MAX_LENGTH,
  FactsTable,
  normalizeFactValue,
  truncateFactValue,
} from "./FactsTable";
import {
  dedupeFacts,
  fieldLabel,
  formatFactFieldValue,
  humanizeSubject,
  policyLabel,
} from "./fact-humanize";

afterEach(() => cleanup());

// antd Table (rc-responsive) requires matchMedia, which jsdom does not implement.
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

/** Crash payload shape reported in ISSUE-4: object/array/number/string mix. */
const crashFacts = [
  {
    fe_id: "f1",
    subject: "Chuẩn button bàn giao",
    policy_key: "camellia.policy.htls",
    fields: {
      htls: { loai: "HBTN", dien_tich: "87 m²" },
      chuan: ["thô", "hoàn thiện phần cơ bản"],
      som95: 95,
      thanh_thoi: "t".repeat(400),
    },
  },
] as unknown as FactEvidence[];

function spyConsoleError() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}

describe("normalizeFactValue (pure)", () => {
  it("returns null for null/undefined", () => {
    expect(normalizeFactValue(null)).toBeNull();
    expect(normalizeFactValue(undefined)).toBeNull();
  });
  it("passes scalars through as strings", () => {
    expect(normalizeFactValue(42)).toBe("42");
    expect(normalizeFactValue("mặt bằng 87 m²")).toBe("mặt bằng 87 m²");
  });
  it("stringifies objects deterministically (JSON key order)", () => {
    const obj = { b: 2, a: 1 };
    expect(normalizeFactValue(obj)).toBe('{"b":2,"a":1}');
    expect(normalizeFactValue(obj)).toBe(normalizeFactValue({ b: 2, a: 1 }));
  });
  it("stringifies arrays deterministically", () => {
    expect(normalizeFactValue(["thô", "HBTN"])).toBe('["thô","HBTN"]');
  });
  it("handles boolean and cyclic payloads without throwing", () => {
    expect(normalizeFactValue(true)).toBe("true");
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(typeof normalizeFactValue(cyclic)).toBe("string");
  });
  it("caps length at FACT_VALUE_MAX_LENGTH with an ellipsis", () => {
    const long = normalizeFactValue("x".repeat(1000));
    expect(long).toHaveLength(FACT_VALUE_MAX_LENGTH + 1);
    expect(long?.endsWith("…")).toBe(true);
    expect(truncateFactValue("ok")).toBe("ok");
  });
});

describe.each(["table", "cards"] as const)("FactsTable variant=%s crash payload", (variant) => {
  it("renders the ISSUE-4 payload {htls,chuan,som95,thanh_thoi} without crashing", () => {
    const err = spyConsoleError();
    const { container } = render(<FactsTable facts={crashFacts} formatMoney={false} variant={variant} />);
    const text = container.textContent ?? "";
    expect(text).toContain('{"loai":"HBTN","dien_tich":"87 m²"}');
    expect(text).toContain('["thô","hoàn thiện phần cơ bản"]');
    expect(text).toContain("95");
    expect(text).not.toContain("t".repeat(300)); // truncated
    expect(err.mock.calls.some((c) => String(c[0]).includes("Objects are not valid"))).toBe(false);
    expect(
      err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))
    ).toBe(false);
    err.mockRestore();
  });

  it("renders an em-dash placeholder for null values", () => {
    const facts = [{ fe_id: "f2", subject: "S", fields: { giaban: null } }] as FactEvidence[];
    const { container } = render(<FactsTable facts={facts} variant={variant} />);
    expect(container.textContent).toContain("—");
  });

  it("still formats VND for numeric fields when formatMoney is on", () => {
    const facts = [
      { fe_id: "f3", subject: "Giá", fields: { gia: 2100000000 } },
    ] as FactEvidence[];
    const { container } = render(<FactsTable facts={facts} formatMoney variant={variant} />);
    // Numbers keep their existing VND formatting after normalization.
    expect(container.textContent).toMatch(/2[\s,.]?1/);
  });
});

describe("[D1] fact humanization dictionaries (pure)", () => {
  it("maps known policy keys to Vietnamese labels", () => {
    expect(policyLabel("htls")).toBe("Hỗ trợ lãi suất (HTLS)");
    expect(policyLabel("chuan")).toBe("Thanh toán chuẩn");
    expect(policyLabel("som95")).toBe("Thanh toán sớm 95%");
    expect(policyLabel("thanhthoi")).toBe("Thanh toán thảnh thơi");
    expect(policyLabel("thanh_thoi")).toBe("Thanh toán thảnh thơi");
  });
  it("falls back to title-case for unknown policy keys", () => {
    expect(policyLabel("uu_dai_dac_biet")).toBe("Uu Dai Dac Biet");
  });
  it("maps known field keys", () => {
    expect(fieldLabel("interest_rate_pct")).toBe("Lãi suất ưu đãi");
    expect(fieldLabel("term_months")).toBe("Thời hạn (tháng)");
    expect(fieldLabel("price_vnd")).toBe("Giá");
    expect(fieldLabel("monthly_principal_vnd")).toBe("Trả gốc/tháng");
  });
  it("falls back to humanized snake_case for unknown fields", () => {
    expect(fieldLabel("phí_quản_lý")).toBe("Phí Quản Lý");
  });
  it("humanizes unit subjects to friendly names", () => {
    expect(humanizeSubject("unit:camellia/2pn-goc")).toBe("Căn 2PN góc");
    expect(humanizeSubject("unit:camellia/2pn-mat-duong")).toBe("Căn 2PN mặt đường");
    expect(humanizeSubject("unit:camellia/1p1-noi-khu")).toBe("Căn 1PN+1 nội khu");
    expect(humanizeSubject("unit:soleil/studio")).toBe("Căn Studio");
  });
  it("passes non-unit subjects through unchanged", () => {
    expect(humanizeSubject("Chuẩn button bàn giao")).toBe("Chuẩn button bàn giao");
  });
  it("formats percent fields with % and NEVER with đ", () => {
    expect(formatFactFieldValue("interest_rate_pct", "0")).toBe("0%");
    expect(formatFactFieldValue("interest_rate_pct", "3.5")).toBe("3,5%");
    expect(formatFactFieldValue("interest_rate_pct", "0")).not.toContain("đ");
    expect(formatFactFieldValue("deposit_pct", "30")).toBe("30%");
  });
  it("formats month fields with tháng", () => {
    expect(formatFactFieldValue("term_months", "18")).toBe("18 tháng");
  });
  it("leaves vnd fields for the caller's money formatter", () => {
    expect(formatFactFieldValue("price_vnd", "4200000000")).toBe("4200000000");
  });
  it("collapses duplicate (subject, policy_key) rows keeping all fields", () => {
    const merged = dedupeFacts([
      { fe_id: "a", subject: "unit:camellia/2pn-goc", policy_key: "htls", fields: { interest_rate_pct: 0 } },
      { fe_id: "b", subject: "unit:camellia/2pn-goc", policy_key: "htls", fields: { term_months: 18 } },
    ]);
    expect(merged).toHaveLength(1);
    expect(merged[0].fe_id).toBe("a");
    expect(merged[0].fields.interest_rate_pct).toBe(0);
    expect(merged[0].fields.term_months).toBe(18);
  });
  it("keeps distinct subjects/policies separate", () => {
    const merged = dedupeFacts([
      { fe_id: "a", subject: "unit:x/2pn", policy_key: "htls", fields: {} },
      { fe_id: "b", subject: "unit:x/2pn", policy_key: "chuan", fields: {} },
      { fe_id: "c", subject: "unit:x/3pn", policy_key: "htls", fields: {} },
    ]);
    expect(merged).toHaveLength(3);
  });
});

describe.each(["table", "cards"] as const)("FactsTable variant=%s D1 rendering", (variant) => {
  it("renders the OCR-verified garbage payload fully humanized", () => {
    const facts = [
      {
        fe_id: "d1",
        subject: "unit:camellia/2pn-goc",
        policy_key: "htls",
        fields: { interest_rate_pct: 0, term_months: 18 },
      },
    ] as unknown as FactEvidence[];
    const { container } = render(<FactsTable facts={facts} variant={variant} />);
    const text = container.textContent ?? "";
    expect(text).toContain("Căn 2PN góc");
    expect(text).toContain("Hỗ trợ lãi suất (HTLS)");
    expect(text).toContain("Lãi suất ưu đãi");
    expect(text).toContain("0%");
    expect(text).toContain("18 tháng");
    expect(text).not.toContain("0đ");
    expect(text).not.toContain("unit:camellia");
    expect(text).not.toContain("interest_rate_pct");
  });

  it("merges duplicate rows in the rendered table", () => {
    const facts = [
      { fe_id: "r1", subject: "unit:camellia/2pn", policy_key: "htls", fields: { interest_rate_pct: 0 } },
      { fe_id: "r2", subject: "unit:camellia/2pn", policy_key: "htls", fields: { term_months: 18 } },
    ] as unknown as FactEvidence[];
    const { container } = render(<FactsTable facts={facts} variant={variant} />);
    const text = container.textContent ?? "";
    expect(text).toContain("0%");
    expect(text).toContain("18 tháng");
    if (variant === "table") {
      // One merged row: only one subject cell.
      expect(text.split("Căn 2PN").length - 1).toBe(1);
    }
  });
});
