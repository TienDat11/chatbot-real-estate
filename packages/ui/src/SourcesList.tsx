import { List, Typography, Tag } from "antd";
import { FileTextOutlined } from "@ant-design/icons";
import type { Source } from "@rag-ragre/contracts";

export interface SourcesListProps {
  sources: Source[];
  /** Cap the number of sources shown (default: all). */
  max?: number;
}

/**
 * Lists the cited source documents of the answer.
 * Each source shows: document title, clause, effective date, kind.
 *
 * Colors mirror the app's premium proptech tokens (apps/web/src/lib/tokens.ts):
 * navy #0E2A47 primary, warm border #E9E2D6 / surface-alt #F3EFE7 neutrals.
 * The ui package stays dependency-free, so the hex values are kept in sync by
 * convention — change them together.
 */
export function SourcesList({ sources, max }: SourcesListProps) {
  if (!sources.length) return null;
  const visible = max ? sources.slice(0, max) : sources;
  const hiddenCount = max ? Math.max(0, sources.length - max) : 0;

  return (
    <div>
      <List
        size="small"
        dataSource={visible}
        split={false}
        renderItem={(source) => (
          <List.Item style={{ padding: "4px 0", border: "none" }}>
            <div style={{ display: "flex", gap: 8, alignItems: "flex-start", minWidth: 0 }}>
              <FileTextOutlined
                style={{ color: "#0E2A47", marginTop: 3, flexShrink: 0, fontSize: 13 }}
              />
              <div style={{ minWidth: 0 }}>
                <Typography.Text
                  strong
                  style={{ fontSize: 13, color: "#1A2233", display: "block" }}
                  ellipsis={{ tooltip: source.title }}
                >
                  {source.title}
                </Typography.Text>
                {(source.section || source.kind) && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {source.section ? `${source.section} · ` : ""}
                    {source.kind}
                  </Typography.Text>
                )}
                {source.effective_from && (
                  <div style={{ marginTop: 2 }}>
                    <Tag
                      style={{
                        fontSize: 11,
                        lineHeight: "18px",
                        borderRadius: 6,
                        marginInlineEnd: 0,
                        color: "#5B6478",
                        background: "#F3EFE7",
                        border: "1px solid #E9E2D6",
                      }}
                    >
                      Hiệu lực: {source.effective_from}
                    </Tag>
                  </div>
                )}
              </div>
            </div>
          </List.Item>
        )}
      />
      {hiddenCount > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
          + {hiddenCount} nguồn khác
        </Typography.Text>
      )}
    </div>
  );
}
