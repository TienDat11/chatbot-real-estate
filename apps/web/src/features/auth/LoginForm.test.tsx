// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { UserCredential } from "firebase/auth";

// The auth service is the only Firebase-touching seam; mocking it keeps these
// component tests hermetic (no Firebase app, no network, no env vars).
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  signInWithEmail: vi.fn(),
  signUpWithEmail: vi.fn(),
  signOutUser: vi.fn(),
  onAuthChange: vi.fn(() => () => {}),
}));

import { signInWithEmail } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { AuthProvider } from "@/lib/AuthProvider";
import { LoginForm } from "@/features/auth/LoginForm";

const signInWithEmailMock = vi.mocked(signInWithEmail);

function renderLoginForm(onLoginSucceeded?: () => void) {
  return render(
    <AuthProvider>
      <LoginForm onLoginSucceeded={onLoginSucceeded} />
    </AuthProvider>
  );
}

function fillField(labelText: string, value: string) {
  fireEvent.change(screen.getByLabelText(labelText), { target: { value } });
}

async function submitForm() {
  fireEvent.click(screen.getByRole("button", { name: "Đăng nhập" }));
}

describe("LoginForm", () => {
  beforeEach(() => {
    signInWithEmailMock.mockReset();
  });

  // vitest globals are off, so RTL cannot auto-register cleanup itself.
  afterEach(() => {
    cleanup();
  });

  it("validates required fields in Vietnamese and never calls the auth service", async () => {
    renderLoginForm();
    await submitForm();
    expect(await screen.findByText("Vui lòng nhập email.")).toBeTruthy();
    expect(await screen.findByText("Vui lòng nhập mật khẩu.")).toBeTruthy();
    expect(signInWithEmailMock).not.toHaveBeenCalled();
  });

  it("rejects a malformed email address", async () => {
    renderLoginForm();
    fillField("Email", "not-an-email");
    fillField("Mật khẩu", "secret123");
    await submitForm();
    expect(await screen.findByText("Địa chỉ email không hợp lệ.")).toBeTruthy();
    expect(signInWithEmailMock).not.toHaveBeenCalled();
  });

  it("submits the entered credentials to the auth service", async () => {
    signInWithEmailMock.mockResolvedValue({} as Awaited<ReturnType<typeof signInWithEmail>>);
    const onLoginSucceeded = vi.fn();
    renderLoginForm(onLoginSucceeded);
    fillField("Email", "admin@example.com");
    fillField("Mật khẩu", "secret123");
    await submitForm();
    await waitFor(() => expect(onLoginSucceeded).toHaveBeenCalledTimes(1));
    expect(signInWithEmailMock).toHaveBeenCalledWith("admin@example.com", "secret123");
  });

  it("surfaces a friendly Vietnamese message for invalid credentials", async () => {
    signInWithEmailMock.mockRejectedValue({ code: "auth/invalid-credential" });
    renderLoginForm();
    fillField("Email", "admin@example.com");
    fillField("Mật khẩu", "wrong-password");
    await submitForm();
    expect(await screen.findByText("Email hoặc mật khẩu không đúng.")).toBeTruthy();
  });

  it("falls back to the generic message for unknown error codes", async () => {
    signInWithEmailMock.mockRejectedValue({ code: "auth/something-new" });
    renderLoginForm();
    fillField("Email", "admin@example.com");
    fillField("Mật khẩu", "wrong-password");
    await submitForm();
    expect(
      await screen.findByText("Đăng nhập không thành công. Vui lòng thử lại hoặc liên hệ quản trị viên.")
    ).toBeTruthy();
  });

  it("shows the submit button as loading while sign-in is pending, then recovers", async () => {
    let resolveSignIn: (value: UserCredential | PromiseLike<UserCredential>) => void = () => {};
    signInWithEmailMock.mockReturnValue(
      new Promise<UserCredential>((resolve) => {
        resolveSignIn = resolve;
      })
    );
    renderLoginForm();
    fillField("Email", "admin@example.com");
    fillField("Mật khẩu", "secret123");
    await submitForm();
    const submitButton = screen.getByRole("button", { name: /Đăng nhập/i });
    await waitFor(() => expect(submitButton.className).toContain("ant-btn-loading"));
    resolveSignIn({} as UserCredential);
    await waitFor(() => expect(submitButton.className).not.toContain("ant-btn-loading"));
  });
});
