/**
 * Humanization dictionaries + formatters for FactsTable (defect D1).
 * The backend fact store keys subjects/policies/fields in machine space
 * ("unit:camellia/2pn-goc", "htls", "interest_rate_pct"); customers must see
 * friendly Vietnamese labels and unit-aware values ("0%", "18 tháng") instead
 * of raw keys and VND-formatted percentages. Pure functions — unit-testable
 * without a DOM.
 */

/** policy_key -> Vietnamese display label (ground truth: payment_methods.json / business_rules.json). */
const POLICY_LABELS: Record<string, string> = {
  htls: "Hỗ trợ lãi suất (HTLS)",
  chuan: "Thanh toán chuẩn",
  som95: "Thanh toán sớm 95%",
  // Canonical repo spelling is "thanhthoi" (data/_processed/payment_methods.json
  // names it "Phương án thanh toán thảnh thơi"); the underscored legacy variant
  // is mapped too because old ingested facts still carry it.
  thanhthoi: "Thanh toán thảnh thơi",
  thanh_thoi: "Thanh toán thảnh thơi",
  giong_nhau: "Áp dụng chung",
};

/** field key -> Vietnamese display label. */
const FIELD_LABELS: Record<string, string> = {
  interest_rate_pct: "Lãi suất ưu đãi",
  term_months: "Thời hạn (tháng)",
  deposit_pct: "Vốn tự túc (%)",
  price_vnd: "Giá",
  loan_amount_vnd: "Số tiền vay",
  required_down_payment_vnd: "Thanh toán ban đầu",
  monthly_principal_vnd: "Trả gốc/tháng",
  monthly_interest_estimate_vnd: "Lãi dự kiến/tháng",
};

/** Unit token map for `unit:<project>/<tokens>` subject keys. Slugs are
 * ASCII-folded by the ingest pipeline; map folded forms to display text. */
const SUBJECT_TOKENS: Record<string, string> = {
  "1p1": "1PN+1",
  "2pn": "2PN",
  "3pn": "3PN",
  studio: "Studio",
  "mat-duong": "mặt đường",
  "noi-khu": "nội khu",
  goc: "góc",
  duplex: "Duplex",
  penthouse: "Penthouse",
};

/** Field suffix -> unit-aware renderer kind. */
type FieldUnit = "pct" | "months" | "vnd" | "plain";

function fieldUnit(key: string): FieldUnit {
  if (key.endsWith("_pct") || key.endsWith("_percent")) return "pct";
  if (key.endsWith("_months")) return "months";
  if (key.endsWith("_vnd")) return "vnd";
  return "plain";
}

const viNumberFormatter = new Intl.NumberFormat("vi-VN", {
  maximumFractionDigits: 2,
});

/** "interest_rate_pct" -> "Interest Rate Pct"; snake_case title-case fallback. */
export function humanizeFieldKey(key: string): string {
  return key
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** "htls" -> "Htls"; arbitrary policy keys title-case as fallback. */
export function humanizePolicyKey(key: string): string {
  return key
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function policyLabel(key: string): string {
  return POLICY_LABELS[key.toLowerCase()] ?? humanizePolicyKey(key);
}

export function fieldLabel(key: string): string {
  return FIELD_LABELS[key.toLowerCase()] ?? humanizeFieldKey(key);
}

/**
 * "unit:camellia/2pn-mat-duong" -> "Căn 2PN mặt đường".
 * Non-"unit:" subjects pass through unchanged.
 */
export function humanizeSubject(subject: string): string {
  if (!subject.startsWith("unit:")) return subject;
  const tokens = subject.slice("unit:".length).split("/");
  const unitPart = tokens[tokens.length - 1] ?? "";
  // Greedy longest-match tokenization: multi-word slugs ("mat-duong",
  // "noi-khu") must match as a whole, so at every position try the longest
  // hyphen-joined run first and fall back to the single token. Per-token
  // splitting alone would defeat the map ("mat" / "duong" match nothing).
  const parts = unitPart.split("-").filter(Boolean);
  const mapped: string[] = [];
  for (let i = 0; i < parts.length; i++) {
    let matched: string | undefined;
    let span = 0;
    for (let len = parts.length - i; len >= 1; len--) {
      const joined = parts.slice(i, i + len).join("-").toLowerCase();
      if (SUBJECT_TOKENS[joined]) {
        matched = SUBJECT_TOKENS[joined];
        span = len;
        break;
      }
    }
    if (matched) {
      mapped.push(matched);
      i += span - 1;
    } else {
      mapped.push(parts[i]);
    }
  }
  return `Căn ${mapped.join(" ")}`;
}

/**
 * Unit-aware value formatting. Percent fields NEVER render "đ" — the OCR'd
 * customer screenshot showed `interest_rate_pct: 0đ`, the headline defect.
 * Only *_vnd keys route through formatVND.
 */
export function formatFactFieldValue(key: string, value: string): string {
  const unit = fieldUnit(key);
  switch (unit) {
    case "pct": {
      const num = Number(value);
      return `${viNumberFormatter.format(num)}%`;
    }
    case "months":
      return `${value} tháng`;
    case "vnd":
      return value;
    default:
      return value;
  }
}

/**
 * Collapse duplicate fact rows: backend evidence repeats the same
 * (subject, policy_key, field) across answer legs. Keeps the FIRST fe_id per
 * merged row (stable for React keys) and merges field maps (later values win
 * only when the earlier value was null).
 */
export function dedupeFacts<T extends { fe_id: string; subject: string; policy_key?: string; fields: Record<string, unknown> }>(
  facts: T[],
): T[] {
  const seen = new Map<string, T>();
  const order: string[] = [];
  for (const fact of facts) {
    const key = `${fact.subject}::${fact.policy_key ?? ""}`;
    const existing = seen.get(key);
    if (!existing) {
      seen.set(key, { ...fact, fields: { ...fact.fields } });
      order.push(key);
      continue;
    }
    const merged: Record<string, unknown> = { ...existing.fields };
    for (const [field, value] of Object.entries(fact.fields ?? {})) {
      if (merged[field] === null || merged[field] === undefined) merged[field] = value;
    }
    seen.set(key, { ...existing, fields: merged });
  }
  return order.map((key) => seen.get(key)!);
}
