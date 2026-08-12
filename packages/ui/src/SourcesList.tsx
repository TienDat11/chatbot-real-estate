import { List, Typography, Tag } from "antd";
import { FileTextOutlined } from "@ant-design/icons";
import type { Source } from "@rag-ragre/contracts";

export interface SourcesListProps {
  sources: Source[];
  /** Giới hạn số nguồn hiển thị (mặc định hiện hết). */
  max?: number;
}

/**
 * Danh sách nguồn tài liệu trích dẫn của câu trả lời.
 * Mỗi nguồn hiển thị: tên văn bản, mục điều khoản, thời điểm hiệu lực, loại.
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
                style={{ color: "#1F46A8", marginTop: 3, flexShrink: 0, fontSize: 13 }}
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
                        background: "#F4F6FA",
                        border: "1px solid #E9ECF2",
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
