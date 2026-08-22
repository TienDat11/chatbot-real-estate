import { describe, expect, it } from "vitest";
import { mapFirebaseAuthErrorToVietnameseMessage } from "@/features/auth/firebaseAuthErrorMessage";

function firebaseAuthError(code: string) {
  return { code, message: "raw sdk message" };
}

describe("mapFirebaseAuthErrorToVietnameseMessage", () => {
  it("translates the invalid-credential family to a shared wrong-credentials message", () => {
    for (const code of ["auth/invalid-credential", "auth/wrong-password", "auth/user-not-found"]) {
      expect(mapFirebaseAuthErrorToVietnameseMessage(firebaseAuthError(code))).toBe(
        "Email hoặc mật khẩu không đúng."
      );
    }
  });

  it("translates rate limiting and network failures", () => {
    expect(mapFirebaseAuthErrorToVietnameseMessage(firebaseAuthError("auth/too-many-requests"))).toBe(
      "Bạn đã thử đăng nhập quá nhiều lần. Vui lòng thử lại sau ít phút."
    );
    expect(
      mapFirebaseAuthErrorToVietnameseMessage(firebaseAuthError("auth/network-request-failed"))
    ).toBe("Lỗi kết nối mạng. Vui lòng kiểm tra internet và thử lại.");
  });

  it("falls back to a generic message for unmapped codes", () => {
    expect(mapFirebaseAuthErrorToVietnameseMessage(firebaseAuthError("auth/some-future-code"))).toBe(
      "Đăng nhập không thành công. Vui lòng thử lại hoặc liên hệ quản trị viên."
    );
  });

  it("falls back safely for non-Firebase errors (no code property)", () => {
    expect(mapFirebaseAuthErrorToVietnameseMessage(new Error("boom"))).toBe(
      "Đăng nhập không thành công. Vui lòng thử lại hoặc liên hệ quản trị viên."
    );
    expect(mapFirebaseAuthErrorToVietnameseMessage(null)).toBe(
      "Đăng nhập không thành công. Vui lòng thử lại hoặc liên hệ quản trị viên."
    );
  });
});
