/**
 * Admin CMS domain — the six standardized project documents (story 8.4).
 *
 * A new project enters the system as six JSON files (the same shapes the
 * ingest pipeline normalizes into `data/_processed/*.json`). The form parses
 * and structurally validates them client-side so an admin sees fact-mapping
 * problems before anything reaches the backend; the publish/ingest workflow
 * itself arrives with ISSUE-13 (wave 3) and re-validates server-side — these
 * checks are a fast feedback loop, not a security boundary.
 */

export const PROJECT_DOCUMENT_KINDS = [
  "project_info",
  "price_matrix",
  "unit_catalog",
  "payment_methods",
  "sales_contacts",
  "business_rules",
] as const;

export type ProjectDocumentKind = (typeof PROJECT_DOCUMENT_KINDS)[number];

export const PROJECT_DOCUMENT_LABELS: Record<ProjectDocumentKind, string> = {
  project_info: "Thông tin dự án (project_info)",
  price_matrix: "Bảng giá (price_matrix)",
  unit_catalog: "Danh mục căn hộ (unit_catalog)",
  payment_methods: "Phương thức thanh toán (payment_methods)",
  sales_contacts: "Đầu mối sales (sales_contacts)",
  business_rules: "Quy tắc kinh doanh (business_rules)",
};

/** Same key shape ISSUE-13's publish endpoint will enforce server-side. */
export const PROJECT_KEY_PATTERN = /^[a-z0-9_]{2,40}$/;

export interface ParsedProjectDocument {
  kind: ProjectDocumentKind;
  /** Raw parsed body; structural typing only — kinds diverge widely. */
  body: Record<string, unknown>;
}

export interface ProjectDocumentIssue {
  kind: ProjectDocumentKind;
  message: string;
}

export interface ProjectFactPreview {
  /** One row per uploaded document: what the ingest will map out of it. */
  rows: { kind: ProjectDocumentKind; recordCount: number; summary: string }[];
}

export interface ProjectDraftValidationResult {
  errors: ProjectDocumentIssue[];
  preview: ProjectFactPreview | null;
}

const REQUIRED_TOP_LEVEL_KEYS_BY_KIND: Record<ProjectDocumentKind, string[]> = {
  project_info: ["project", "ten_phap_ly", "ten_thuong_mai", "vi_tri"],
  price_matrix: ["project", "types"],
  unit_catalog: ["project", "units"],
  payment_methods: ["project", "methods"],
  sales_contacts: ["project", "contacts"],
  business_rules: ["project", "rules"],
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function arrayLength(body: Record<string, unknown>, key: string): number {
  const value = body[key];
  return Array.isArray(value) ? value.length : 0;
}

function summarizeDocument(kind: ProjectDocumentKind, body: Record<string, unknown>): string {
  switch (kind) {
    case "project_info":
      return `Legal name: ${String(body.ten_phap_ly ?? "?")} — location: ${String(body.vi_tri ?? "?")}`;
    case "price_matrix":
      return `${arrayLength(body, "types")} price types`;
    case "unit_catalog":
      return `${arrayLength(body, "units")} units catalogued`;
    case "payment_methods":
      return `${arrayLength(body, "methods")} payment methods`;
    case "sales_contacts":
      return `${arrayLength(body, "contacts")} contacts`;
    case "business_rules":
      return `${arrayLength(body, "rules")} rules`;
  }
}

/**
 * Parses one uploaded file's text into a validated document of `kind`.
 * Returns the issue list (empty = valid); a parse failure yields exactly one
 * syntax issue. Kept pure (text in, issues out) so tests need no File API.
 */
export function validateProjectDocumentText(
  kind: ProjectDocumentKind,
  documentText: string,
): ProjectDocumentIssue[] {
  let body: unknown;
  try {
    body = JSON.parse(documentText);
  } catch {
    return [{ kind, message: "File không phải JSON hợp lệ." }];
  }
  if (!isPlainObject(body)) {
    return [{ kind, message: "Nội dung file phải là một object JSON." }];
  }
  return REQUIRED_TOP_LEVEL_KEYS_BY_KIND[kind]
    .filter((key) => !(key in body))
    .map((key) => ({ kind, message: `Thiếu trường bắt buộc "${key}".` }));
}

/**
 * Cross-document validation for a full draft: every document must declare the
 * SAME project key, or the ingest would scatter facts across projects.
 */
export function validateProjectDraft(
  draftProjectKey: string,
  documents: Partial<Record<ProjectDocumentKind, ParsedProjectDocument>>,
): ProjectDraftValidationResult {
  const errors: ProjectDocumentIssue[] = [];
  if (!PROJECT_KEY_PATTERN.test(draftProjectKey)) {
    errors.push({
      kind: "project_info",
      message:
        "Mã dự án chỉ gồm chữ thường, số và dấu gạch dưới (2–40 ký tự), ví dụ: soleil_riverside.",
    });
  }

  const presentKinds = PROJECT_DOCUMENT_KINDS.filter((kind) => documents[kind]);
  if (presentKinds.length < PROJECT_DOCUMENT_KINDS.length) {
    const missing = PROJECT_DOCUMENT_KINDS.filter((kind) => !documents[kind]).map(
      (kind) => PROJECT_DOCUMENT_LABELS[kind],
    );
    errors.push({ kind: "project_info", message: `Còn thiếu file: ${missing.join(", ")}.` });
    return { errors, preview: null };
  }

  for (const kind of presentKinds) {
    const declaredProjectKey = documents[kind]?.body.project;
    if (declaredProjectKey !== draftProjectKey) {
      errors.push({
        kind,
        message: `Trường "project" trong file (${String(declaredProjectKey)}) không khớp mã dự án (${draftProjectKey}).`,
      });
    }
  }

  // A draft that fails any check has no trustworthy fact mapping to preview —
  // showing rows for a cross-project mismatch would suggest the mapping is
  // usable, so the preview stays hidden until the draft is fully consistent.
  if (errors.length > 0) {
    return { errors, preview: null };
  }

  const preview: ProjectFactPreview = {
    rows: presentKinds.map((kind) => {
      const body = documents[kind]!.body;
      return {
        kind,
        recordCount: arrayLength(body, arrayKeyForKind(kind)),
        summary: summarizeDocument(kind, body),
      };
    }),
  };
  return { errors, preview };
}

function arrayKeyForKind(kind: ProjectDocumentKind): string {
  switch (kind) {
    case "price_matrix":
      return "types";
    case "unit_catalog":
      return "units";
    case "payment_methods":
      return "methods";
    case "sales_contacts":
      return "contacts";
    case "business_rules":
      return "rules";
    case "project_info":
      // project_info has no enumerable collection — its preview row counts 0
      // and the summary line carries the identity mapping instead.
      return "";
  }
}
