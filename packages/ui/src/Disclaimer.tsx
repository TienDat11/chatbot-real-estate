import { Typography } from "antd";

/**
 * Footer disclaimer: AI hỗ trợ tra cứu, không phải tư vấn pháp lý chính thức.
 * Luôn hiển thị ở đáy trang để đáp ứng yêu cầu trust của sản phẩm legal.
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
