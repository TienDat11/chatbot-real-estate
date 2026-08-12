import { Alert } from "antd";

/**
 * Banner cảnh báo khi câu trả lời LOW confidence hoặc chứa keyword
 * high-stakes (cầm cố, thế chấp, tranh chấp...) — cần tư vấn viên
 * xác nhận trước khi phản hồi khách hàng.
 */
export function ReviewBanner() {
  return (
    <Alert
      type="warning"
      showIcon
      message="Câu trả lời cần tư vấn viên xác nhận"
      description="Nội dung này có độ tin cậy thấp hoặc liên quan tình huống rủi ro cao. Vui lòng đối chiếu với văn bản gốc và tư vấn viên pháp lý trước khi chuyển tới khách hàng."
      style={{
        borderRadius: 12,
        border: "1px solid #F3D9A4",
        background: "#FFF8EC",
      }}
    />
  );
}
