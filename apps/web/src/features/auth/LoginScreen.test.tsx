// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AuthenticatedUser } from "@/infrastructure/firebase/firebaseAuthenticationService";

// Router doubles: navigation is asserted against the replace mock, and the
// search params double carries the `next` value under test.
const replaceMock = vi.fn();
let currentSearchParams: URLSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => currentSearchParams,
}));

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  signInWithEmail: vi.fn(),
  signUpWithEmail: vi.fn(),
  signOutUser: vi.fn(),
  onAuthChange: vi.fn((callback: (user: AuthenticatedUser | null) => void) => {
    capturedAuthChangeCallback = callback;
    return () => {};
  }),
}));

import { AuthProvider } from "@/lib/AuthProvider";
import { LoginScreen } from "@/features/auth/LoginScreen";
import type { Role } from "@/domain/auth/role";

let capturedAuthChangeCallback: ((user: AuthenticatedUser | null) => void) | null = null;

function authenticatedUser(role: Role): AuthenticatedUser {
  return { uid: "u1", email: "a@b.vn", displayName: null, photoURL: null, role };
}

function renderLoginScreen(nextParam: string | null) {
  currentSearchParams = nextParam === null ? new URLSearchParams() : new URLSearchParams({ next: nextParam });
  render(
    <AuthProvider>
      <LoginScreen />
    </AuthProvider>
  );
}

describe("LoginScreen redirect logic", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    capturedAuthChangeCallback = null;
  });

  // vitest globals are off, so RTL cannot auto-register cleanup itself.
  afterEach(() => {
    cleanup();
  });

  it("does not silently redirect an existing admin session away from login", async () => {
    renderLoginScreen(null);
    capturedAuthChangeCallback!(authenticatedUser("admin"));
    await screen.findByText("Bạn đang đăng nhập. Nhập lại thông tin để xác thực tài khoản hiện tại.");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("keeps an existing staff session on login with its requested return path", async () => {
    renderLoginScreen("/admin/leads?project=soleil");
    capturedAuthChangeCallback!(authenticatedUser("sales"));
    await screen.findByText("Bạn đang đăng nhập. Nhập lại thông tin để xác thực tài khoản hiện tại.");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("does not silently redirect an existing viewer session", async () => {
    renderLoginScreen("/admin");
    capturedAuthChangeCallback!(authenticatedUser("viewer"));
    await screen.findByText("Bạn đang đăng nhập. Nhập lại thông tin để xác thực tài khoản hiện tại.");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("does not navigate while nobody is signed in", () => {
    renderLoginScreen(null);
    capturedAuthChangeCallback!(null);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("keeps an existing session on the login screen until the user intentionally submits", async () => {
    renderLoginScreen("/project/soleil?session=s1");
    capturedAuthChangeCallback!(authenticatedUser("admin"));

    expect(await screen.findByText("Bạn đang đăng nhập. Nhập lại thông tin để xác thực tài khoản hiện tại.")).toBeTruthy();
    expect(replaceMock).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "a@b.vn" } });
    fireEvent.change(screen.getByLabelText("Mật khẩu"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Đăng nhập" }));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/project/soleil?session=s1"));
  });

  it("renders the login form heading for not-yet-authenticated visitors", () => {
    renderLoginScreen(null);
    expect(screen.getByText("Đăng nhập hệ thống")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Đăng nhập" })).toBeTruthy();
  });
});
