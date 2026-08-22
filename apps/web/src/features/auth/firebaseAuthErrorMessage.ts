/**
 * Translates Firebase email/password error codes into friendly Vietnamese UI
 * text. Kept as a pure function so the form stays thin and the mapping is
 * unit-testable without a Firebase connection.
 */

/** Minimal structural type of a Firebase auth error; avoids importing SDK types into features. */
export interface FirebaseAuthErrorCode {
  code?: string;
}

const VIETNAMESE_AUTH_ERROR_MESSAGES: Record<string, string> = {
  "auth/invalid-email": "Địa chỉ email không hợp lệ.",
  "auth/user-disabled": "Tài khoản đã bị vô hiệu hóa. Vui lòng liên hệ quản trị viên.",
  "auth/user-not-found": "Email hoặc mật khẩu không đúng.",
  "auth/wrong-password": "Email hoặc mật khẩu không đúng.",
  "auth/invalid-credential": "Email hoặc mật khẩu không đúng.",
  "auth/missing-password": "Vui lòng nhập mật khẩu.",
  "auth/too-many-requests": "Bạn đã thử đăng nhập quá nhiều lần. Vui lòng thử lại sau ít phút.",
  "auth/network-request-failed": "Lỗi kết nối mạng. Vui lòng kiểm tra internet và thử lại.",
  "auth/operation-not-allowed":
    "Đăng nhập bằng email/mật khẩu chưa được bật. Vui lòng liên hệ quản trị viên.",
};

/** Fallback for unmapped codes so the user never sees a raw SDK message. */
const GENERIC_AUTH_ERROR_MESSAGE =
  "Đăng nhập không thành công. Vui lòng thử lại hoặc liên hệ quản trị viên.";

/** Maps a caught sign-in error to Vietnamese UI text (code-first, safe fallback). */
export function mapFirebaseAuthErrorToVietnameseMessage(error: unknown): string {
  const errorCode = (error as FirebaseAuthErrorCode | null)?.code;
  return (
    (typeof errorCode === "string" && VIETNAMESE_AUTH_ERROR_MESSAGES[errorCode]) ||
    GENERIC_AUTH_ERROR_MESSAGE
  );
}
