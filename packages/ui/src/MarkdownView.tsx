import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Typography } from "antd";

export interface MarkdownViewProps {
  content: string;
  className?: string;
}

/**
 * Render markdown (GFM) với style khớp design system: đề mục xanh navy,
 * bảng có border nhẹ, code block nền xám. Dùng cho nội dung câu trả lời.
 */
export function MarkdownView({ content, className }: MarkdownViewProps) {
  return (
    <Typography style={{ fontSize: 14, lineHeight: "24px" }} className={className}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
        {content}
      </ReactMarkdown>
    </Typography>
  );
}

const mdComponents = {
  h1: ({ children }: { children?: React.ReactNode }) => (
    <h1 style={{ fontSize: 20, fontWeight: 700, color: "#1F46A8", margin: "16px 0 8px" }}>{children}</h1>
  ),
  h2: ({ children }: { children?: React.ReactNode }) => (
    <h2 style={{ fontSize: 17, fontWeight: 700, color: "#1F46A8", margin: "14px 0 6px" }}>{children}</h2>
  ),
  h3: ({ children }: { children?: React.ReactNode }) => (
    <h3 style={{ fontSize: 15, fontWeight: 600, color: "#1A2233", margin: "12px 0 4px" }}>{children}</h3>
  ),
  ul: ({ children }: { children?: React.ReactNode }) => (
    <ul style={{ margin: "6px 0 6px 20px", paddingLeft: 0 }}>{children}</ul>
  ),
  ol: ({ children }: { children?: React.ReactNode }) => (
    <ol style={{ margin: "6px 0 6px 20px", paddingLeft: 0 }}>{children}</ol>
  ),
  li: ({ children }: { children?: React.ReactNode }) => (
    <li style={{ margin: "2px 0" }}>{children}</li>
  ),
  table: ({ children }: { children?: React.ReactNode }) => (
    <div style={{ overflowX: "auto", margin: "8px 0" }}>
      <table
        style={{
          borderCollapse: "collapse",
          width: "100%",
          border: "1px solid #E9ECF2",
          borderRadius: 8,
          fontSize: 13,
        }}
      >
        {children}
      </table>
    </div>
  ),
  th: ({ children }: { children?: React.ReactNode }) => (
    <th
      style={{
        border: "1px solid #E9ECF2",
        background: "#F4F6FA",
        padding: "6px 10px",
        textAlign: "left",
        fontWeight: 600,
      }}
    >
      {children}
    </th>
  ),
  td: ({ children }: { children?: React.ReactNode }) => (
    <td style={{ border: "1px solid #E9ECF2", padding: "6px 10px", fontVariantNumeric: "tabular-nums" }}>
      {children}
    </td>
  ),
  code: ({ children }: { children?: React.ReactNode }) => (
    <code
      style={{
        background: "#F4F6FA",
        border: "1px solid #E9ECF2",
        borderRadius: 4,
        padding: "1px 5px",
        fontSize: "12.5px",
        color: "#1F46A8",
      }}
    >
      {children}
    </code>
  ),
  pre: ({ children }: { children?: React.ReactNode }) => (
    <pre
      style={{
        background: "#F4F6FA",
        border: "1px solid #E9ECF2",
        borderRadius: 8,
        padding: 10,
        overflowX: "auto",
        fontSize: 12.5,
        lineHeight: "20px",
      }}
    >
      {children}
    </pre>
  ),
  a: ({ children, href }: { children?: React.ReactNode; href?: string }) => (
    <a href={href} target="_blank" rel="noreferrer" style={{ color: "#1F46A8" }}>
      {children}
    </a>
  ),
  blockquote: ({ children }: { children?: React.ReactNode }) => (
    <blockquote
      style={{
        margin: "8px 0",
        padding: "4px 12px",
        borderLeft: "3px solid #1F46A8",
        background: "#F4F6FA",
        borderRadius: 4,
        color: "#5B6478",
      }}
    >
      {children}
    </blockquote>
  ),
};
