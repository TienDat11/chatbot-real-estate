import { Typography } from "antd";

/**
 * Footer disclaimer: AI-assisted lookup, not formal legal advice.
 * Always visible at the page footer to satisfy the legal product's trust requirement.
 */
export function Disclaimer() {
  return (
    <Typography.Paragraph
      style={{
        textAlign: "center",
        color: "#8A93A6",
        fontSize: 12,
        margin: 0,
        lineHeight: "18px",
      }}
    >
      Kết quả do AI hỗ trợ tra cứu, có thể có sai sót — không phải tư vấn pháp lý chính thức.
      Vui lòng đối chiếu văn bản gốc hoặc tham khảo tư vấn viên.
    </Typography.Paragraph>
  );
}
