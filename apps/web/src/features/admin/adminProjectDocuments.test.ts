import { describe, expect, it } from "vitest";
import {
  PROJECT_DOCUMENT_KINDS,
  validateProjectDocumentText,
  validateProjectDraft,
} from "./adminProjectDocuments";

const VALID_BODIES: Record<string, Record<string, unknown>> = {
  project_info: {
    project: "soleil_riverside",
    ten_phap_ly: "CTCP Đầu tư Soleil",
    ten_thuong_mai: "Soleil Riverside",
    vi_tri: "Q.7, TP.HCM",
  },
  price_matrix: { project: "soleil_riverside", types: [{}, {}] },
  unit_catalog: { project: "soleil_riverside", units: [{}, {}, {}] },
  payment_methods: { project: "soleil_riverside", methods: [{}] },
  sales_contacts: { project: "soleil_riverside", contacts: [{}, {}] },
  business_rules: { project: "soleil_riverside", rules: [{}] },
};

describe("validateProjectDocumentText", () => {
  it("accepts a structurally complete document", () => {
    const issues = validateProjectDocumentText(
      "project_info",
      JSON.stringify(VALID_BODIES.project_info),
    );
    expect(issues).toEqual([]);
  });

  it("reports a syntax issue for non-JSON content", () => {
    const issues = validateProjectDocumentText("price_matrix", "{khong phai json");
    expect(issues).toHaveLength(1);
    expect(issues[0].kind).toBe("price_matrix");
  });

  it("names every missing required key", () => {
    const issues = validateProjectDocumentText(
      "project_info",
      JSON.stringify({ project: "x" }),
    );
    expect(issues.map((issue) => issue.message)).toEqual(
      expect.arrayContaining([
        'Thiếu trường bắt buộc "ten_phap_ly".',
        'Thiếu trường bắt buộc "ten_thuong_mai".',
        'Thiếu trường bắt buộc "vi_tri".',
      ]),
    );
  });
});

describe("validateProjectDraft", () => {
  function parsedAll(projectKey = "soleil_riverside") {
    return Object.fromEntries(
      PROJECT_DOCUMENT_KINDS.map((kind) => [
        kind,
        { kind, body: { ...VALID_BODIES[kind], project: projectKey } },
      ]),
    );
  }

  it("rejects a malformed project key", () => {
    const result = validateProjectDraft("Soleil Riverside!", {});
    expect(result.errors.some((e) => e.message.includes("Mã dự án"))).toBe(true);
    expect(result.preview).toBeNull();
  });

  it("lists every missing file before producing a preview", () => {
    const documents = { project_info: { kind: "project_info" as const, body: VALID_BODIES.project_info } };
    const result = validateProjectDraft("soleil_riverside", documents);
    expect(result.preview).toBeNull();
    expect(
      result.errors.some((issue) => issue.message.includes("Còn thiếu file")),
    ).toBe(true);
  });

  it("flags documents whose declared project differs from the draft key", () => {
    const documents = parsedAll();
    documents.unit_catalog = { kind: "unit_catalog", body: { ...VALID_BODIES.unit_catalog, project: "camellia" } };
    const result = validateProjectDraft("soleil_riverside", documents);
    expect(
      result.errors.some(
        (issue) =>
          issue.kind === "unit_catalog" && issue.message.includes("camellia"),
      ),
    ).toBe(true);
    // Cross-key mismatch must still block, even though all files are present.
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it("builds the fact-mapping preview for a fully consistent draft", () => {
    const result = validateProjectDraft("soleil_riverside", parsedAll());
    expect(result.errors).toEqual([]);
    expect(result.preview?.rows).toHaveLength(6);
    const unitRow = result.preview!.rows.find((row) => row.kind === "unit_catalog");
    expect(unitRow?.recordCount).toBe(3);
    expect(unitRow?.summary).toContain("3 units");
  });
});
