import type { ReactNode } from "react";
import { Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { FactEvidence } from "@rag-ragre/contracts";
import { formatVND } from "@rag-ragre/contracts";
import {
  dedupeFacts,
  fieldLabel,
  formatFactFieldValue,
  humanizeSubject,
  policyLabel,
} from "./fact-humanize";

export interface FactsTableProps {
  facts: FactEvidence[];
  /** When true, VND-money-shaped values are formatted via formatVND. */
  formatMoney?: boolean;
  /**
   * "table" (default): 3-column table — used in the assistant bubble.
   * "cards": compact stacked cards — used in the narrow evidence rail.
   */
  variant?: "table" | "cards";
}

/** Legal facts with citations, tabular-nums aligned. */
// Cards-variant colors mirror the app's premium proptech tokens
// (apps/web/src/lib/tokens.ts): warm surface-alt #F3EFE7 / border #E9E2D6,
// navy #0E2A47 primary on a tinted navy chip #E8EEF7. The ui package stays
// dependency-free, so hex values are kept in sync by convention.
export function FactsTable({ facts, formatMoney = true, variant = "table" }: FactsTableProps) {
  // D1: the evidence backend repeats the same (subject, policy) row across
  // answer legs; customers must see one merged row, not duplicates.
  const visibleFacts = dedupeFacts(facts);
  if (!visibleFacts.length) return null;

  if (variant === "cards") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {visibleFacts.map((fact) => (
          <div
            key={fact.fe_id}
            style={{
              border: "1px solid #E9E2D6",
              borderRadius: 10,
              padding: "8px 10px",
              background: "#F3EFE7",
            }}
          >
            <Typography.Text strong style={{ fontSize: 12.5, color: "#1A2233", display: "block" }}>
              {humanizeSubject(fact.subject)}
            </Typography.Text>
            {fact.policy_key && (
              <code style={{ fontSize: 11.5, color: "#0E2A47", background: "#E8EEF7", padding: "0 5px", borderRadius: 4 }}>
                {policyLabel(fact.policy_key)}
              </code>
            )}
            <div style={{ marginTop: 4 }}>
              {Object.entries(fact.fields ?? {}).map(([key, value]) => (
                <div
                  key={key}
                  style={{
                    display: "flex",
                    gap: 6,
                    fontSize: 12,
                    lineHeight: "18px",
                    flexWrap: "wrap",
                  }}
                >
                  <span style={{ color: "#5B6478", flexShrink: 0 }}>{fieldLabel(key)}:</span>
                  <span
                    style={{
                      fontVariantNumeric: "tabular-nums",
                      color: "#1A2233",
                      wordBreak: "break-word",
                    }}
                  >
                    {formatField(key, value, formatMoney)}
                  </span>
                </div>
              ))}
            </div>
            {fact.note && (
              <Typography.Text type="secondary" style={{ fontSize: 11.5, display: "block", marginTop: 2 }}>
                {normalizeFactValue(fact.note)}
              </Typography.Text>
            )}
          </div>
        ))}
      </div>
    );
  }

  const columns: ColumnsType<FactEvidence> = [
    {
      title: "Sự kiện / tình huống",
      dataIndex: "subject",
      key: "subject",
      render: (value: string) => (
        <Typography.Text strong style={{ fontSize: 13 }}>
          {humanizeSubject(value)}
        </Typography.Text>
      ),
    },
    {
      title: "Điều khoản liên quan",
      dataIndex: "policy_key",
      key: "policy_key",
      render: (value?: string) =>
        value ? (
          <Typography.Text style={{ fontSize: 13, color: "#0E2A47" }}>
            {policyLabel(value)}
          </Typography.Text>
        ) : (
          <span style={{ color: "#B0B7C6" }}>—</span>
        ),
    },
    {
      title: "Chi tiết",
      dataIndex: "fields",
      key: "fields",
      render: (fields: Record<string, unknown>, record) => {
        const entries = Object.entries(fields ?? {});
        const note = normalizeFactValue(record.note);
        if (!entries.length && !note) {
          return <span style={{ color: "#B0B7C6" }}>—</span>;
        }
        return (
          <div>
            {entries.map(([key, value]) => (
              <div
                key={key}
                style={{
                  display: "flex",
                  gap: 8,
                  fontSize: 13,
                  lineHeight: "20px",
                  flexWrap: "wrap",
                }}
              >
                <span style={{ color: "#5B6478", flexShrink: 0 }}>{fieldLabel(key)}:</span>
                <span
                  style={{
                    fontVariantNumeric: "tabular-nums",
                    color: "#1A2233",
                    fontWeight: value !== null ? 500 : 400,
                    wordBreak: "break-word",
                  }}
                >
                  {formatField(key, value, formatMoney)}
                </span>
              </div>
            ))}
            {note && (
              <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 2 }}>
                {note}
              </Typography.Text>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div style={{ overflowX: "auto", maxWidth: "100%" }}>
      <Table<FactEvidence>
        rowKey="fe_id"
        columns={columns}
        dataSource={visibleFacts}
        size="small"
        pagination={false}
        style={{ fontSize: 13 }}
        tableLayout="auto"
      />
    </div>
  );
}

/**
 * Format field value with unit awareness (defect D1): *_pct / *_months keys
 * never route through formatVND (a percent rendered as "0đ" was the headline
 * customer-visible garbage). Only money-shaped numeric values on non-unit
 * keys use formatVND.
 */
function formatField(key: string, value: unknown, formatMoney: boolean): string | ReactNode {
  const normalized = normalizeFactValue(value);
  if (normalized === null) return <span style={{ color: "#B0B7C6" }}>—</span>;
  // Unit-suffixed keys own their formatting regardless of payload type.
  if (typeof value === "number" || /^\d+(\.\d+)?$/.test(normalized)) {
    const unitAware = formatFactFieldValue(key, normalized);
    if (unitAware !== normalized) return unitAware;
  }
  if (typeof value === "number") {
    return formatMoney ? formatVND(value) : normalized;
  }
  // Numeric-looking string (from backend JSON) -> treat as price when key hints money.
  if (formatMoney && /(giá|tiền|phí|thuế|giá_trị)/i.test(key) && /^\d+(\.\d+)?$/.test(normalized)) {
    return formatVND(Number(normalized));
  }
  return normalized;
}

/**
 * Max characters for a normalized fact cell — guards the evidence rail and
 * table cells from pathological payloads (deep objects, huge arrays).
 */
export const FACT_VALUE_MAX_LENGTH = 200;

/**
 * Normalize any fact payload into a display scalar before it reaches React
 * children. Fact fields are typed `number | string | null`, but real backend
 * payloads (e.g. `{htls, chuan, som95, thanh_thoi}` shapes) sometimes carry
 * nested objects or arrays; passing those straight to JSX crashes with
 * "Objects are not valid as a React child". Pure and deterministic: identical
 * input always yields the identical string (JSON.stringify key order), and
 * output longer than FACT_VALUE_MAX_LENGTH is truncated with an ellipsis.
 *
 * Returns null only for null/undefined (callers render a dash placeholder).
 */
export function normalizeFactValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  let text: string;
  if (typeof value === "string") {
    text = value;
  } else if (typeof value === "number") {
    text = String(value);
  } else {
    try {
      text = JSON.stringify(value);
    } catch {
      // Cyclic or otherwise unserializable payload — degrade to String().
      text = String(value);
    }
  }
  return truncateFactValue(text);
}

/** Pure length cap shared by normalizeFactValue (exported for tests). */
export function truncateFactValue(text: string): string {
  const trimmed = text.trim();
  return trimmed.length <= FACT_VALUE_MAX_LENGTH
    ? trimmed
    : `${trimmed.slice(0, FACT_VALUE_MAX_LENGTH)}…`;
}
