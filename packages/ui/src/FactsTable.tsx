import type { ReactNode } from "react";
import { Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { FactEvidence } from "@rag-ragre/contracts";
import { formatVND } from "@rag-ragre/contracts";

export interface FactsTableProps {
  facts: FactEvidence[];
  /** Nếu true, giá trị khớp chuỗi tiền VND sẽ được format qua formatVND. */
  formatMoney?: boolean;
  /**
   * "table" (mặc định): bảng 3 cột — dùng trong bong bóng assistant.
   * "cards": thẻ compact xếp dọc — dùng trong rail dẫn chứng hẹp.
   */
  variant?: "table" | "cards";
}

/**
 * Hiển thị các sự kiện pháp lý (fact) kèm dẫn chứng của câu trả lời.
 * Dùng tabular-nums để số liệu thẳng hàng; border mảnh, header nền nhạt.
 */
export function FactsTable({ facts, formatMoney = true, variant = "table" }: FactsTableProps) {
  if (!facts.length) return null;

  if (variant === "cards") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {facts.map((fact) => (
          <div
            key={fact.fe_id}
            style={{
              border: "1px solid #E9ECF2",
              borderRadius: 10,
              padding: "8px 10px",
              background: "#FAFBFD",
            }}
          >
            <Typography.Text strong style={{ fontSize: 12.5, color: "#1A2233", display: "block" }}>
              {fact.subject}
            </Typography.Text>
            {fact.policy_key && (
              <code style={{ fontSize: 11.5, color: "#1F46A8", background: "#EEF2FA", padding: "0 5px", borderRadius: 4 }}>
                {fact.policy_key}
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
                  <span style={{ color: "#5B6478", flexShrink: 0 }}>{key}:</span>
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
                {fact.note}
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
          {value}
        </Typography.Text>
      ),
    },
    {
      title: "Điều khoản liên quan",
      dataIndex: "policy_key",
      key: "policy_key",
      render: (value?: string) => (value ? <code>{value}</code> : <span style={{ color: "#B0B7C6" }}>—</span>),
    },
    {
      title: "Chi tiết",
      dataIndex: "fields",
      key: "fields",
      render: (fields: Record<string, number | string | null>, record) => {
        const entries = Object.entries(fields ?? {});
        if (!entries.length && !record.note) {
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
                <span style={{ color: "#5B6478", flexShrink: 0 }}>{key}:</span>
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
            {record.note && (
              <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 2 }}>
                {record.note}
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
        dataSource={facts}
        size="small"
        pagination={false}
        style={{ fontSize: 13 }}
        tableLayout="auto"
      />
    </div>
  );
}

/** Format giá trị: kiểu chuỗi trông như số tiền VND -> formatVND; số -> formatVND. */
function formatField(
  key: string,
  value: number | string | null,
  formatMoney: boolean
): string | ReactNode {
  if (value === null || value === undefined) return <span style={{ color: "#B0B7C6" }}>—</span>;
  if (typeof value === "number") {
    return formatMoney ? formatVND(value) : String(value);
  }
  // Chuỗi dạng số thuần (từ backend JSON) -> coi như số tiền khi key gợi ý tiền.
  const looksLikeMoney = /(giá|tiền|phí|thuế|giá_trị)/i.test(key);
  if (formatMoney && looksLikeMoney && /^\d+(\.\d+)?$/.test(value)) {
    return formatVND(Number(value));
  }
  return value;
}
