// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

  it("redirects an admin to /admin by default", async () => {
    renderLoginScreen(null);
    capturedAuthChangeCallback!(authenticatedUser("admin"));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledTimes(1));
    expect(replaceMock).toHaveBeenCalledWith("/admin");
  });

  it("redirects to the safe `next` param for admin/sales", async () => {
    renderLoginScreen("/admin/leads?project=soleil");
    capturedAuthChangeCallback!(authenticatedUser("sales"));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/admin/leads?project=soleil"));
  });

  it("redirects viewer to the public chat even with an admin next param", async () => {
    renderLoginScreen("/admin");
    capturedAuthChangeCallback!(authenticatedUser("viewer"));
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/"));
  });

  it("does not navigate while nobody is signed in", () => {
    renderLoginScreen(null);
    capturedAuthChangeCallback!(null);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("renders the login form heading for not-yet-authenticated visitors", () => {
    renderLoginScreen(null);
    expect(screen.getByText("Đăng nhập hệ thống")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Đăng nhập" })).toBeTruthy();
  });
});
