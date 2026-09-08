// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { AuthenticatedUser } from "@/infrastructure/firebase/firebaseAuthenticationService";

// Mocking the auth service (not AuthProvider) exercises the real context +
// guard wiring; the captured callback lets each test drive auth transitions.
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  signInWithEmail: vi.fn(),
  signUpWithEmail: vi.fn(),
  signOutUser: vi.fn(),
  onAuthChange: vi.fn((callback: (user: AuthenticatedUser | null) => void) => {
    capturedAuthChangeCallback = callback;
    return () => {};
  }),
}));

// App-router navigation is mocked so the guard's signed-out redirect contract
// (login?next=<original path+query>) is observable without a Next runtime.
const navigation = vi.hoisted(() => ({
  replace: vi.fn(),
  pathname: "/admin",
  search: "foo=bar",
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace }),
  usePathname: () => navigation.pathname,
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

import { onAuthChange } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { AuthProvider } from "@/lib/AuthProvider";
import { RequireRole } from "@/components/RequireRole";
import type { Role } from "@/domain/auth/role";

let capturedAuthChangeCallback: ((user: AuthenticatedUser | null) => void) | null = null;

function authenticatedUser(role: Role): AuthenticatedUser {
  return { uid: "u1", email: "a@b.vn", displayName: null, photoURL: null, role };
}

function renderGuard(allowedRoles: Role[]) {
  return render(
    <AuthProvider>
      <RequireRole allowedRoles={allowedRoles}>
        <div data-testid="protected-content">Nội dung quản trị</div>
      </RequireRole>
    </AuthProvider>
  );
}

describe("RequireRole", () => {
  beforeEach(() => {
    capturedAuthChangeCallback = null;
    vi.mocked(onAuthChange).mockClear();
    navigation.replace.mockClear();
    navigation.pathname = "/admin";
    navigation.search = "foo=bar";
  });

  afterEach(() => {
    cleanup();
  });

  it("shows a spinner (never a 403 flash) while auth state is loading", () => {
    // No auth callback fired yet => AuthProvider is still in loading state.
    renderGuard(["admin"]);
    expect(document.querySelector(".ant-spin")).toBeTruthy();
    expect(screen.queryByText("403")).toBeNull();
    expect(screen.queryByTestId("protected-content")).toBeNull();
  });

  it("renders children when the current role is allow-listed", async () => {
    renderGuard(["admin", "sales"]);
    capturedAuthChangeCallback!(authenticatedUser("sales"));
    expect(await screen.findByTestId("protected-content")).toBeTruthy();
    expect(screen.queryByText("403")).toBeNull();
  });

  it("renders a 403 result with a login link for a non-listed role", async () => {
    renderGuard(["admin"]);
    capturedAuthChangeCallback!(authenticatedUser("viewer"));
    expect(await screen.findByText("403")).toBeTruthy();
    expect(screen.queryByTestId("protected-content")).toBeNull();
    const loginButton = screen.getByRole("link", { name: "Đến trang đăng nhập" });
    expect(loginButton.getAttribute("href")).toBe("/login");
  });

  it("redirects a signed-out user to /login preserving path and query in next=", async () => {
    renderGuard(["admin"]);
    capturedAuthChangeCallback!(null);
    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith(
        `/login?next=${encodeURIComponent("/admin?foo=bar")}`
      )
    );
  });

  it("stays fail-closed on 403 for a signed-out user and never redirects them to a 403 shell", async () => {
    renderGuard(["admin"]);
    capturedAuthChangeCallback!(null);
    expect(await screen.findByText("403")).toBeTruthy();
    // The redirect fires as a best-effort navigation; the guard must not treat
    // it as authorization success or render protected content.
    expect(screen.queryByTestId("protected-content")).toBeNull();
  });

  it("renders a 403 result for signed-out users", async () => {
    renderGuard(["admin"]);
    capturedAuthChangeCallback!(null);
    expect(await screen.findByText("403")).toBeTruthy();
  });

  it("keeps an authenticated user without the required role fail-closed at 403 without a login redirect", async () => {
    renderGuard(["admin"]);
    capturedAuthChangeCallback!(authenticatedUser("viewer"));
    expect(await screen.findByText("403")).toBeTruthy();
    expect(navigation.replace).not.toHaveBeenCalled();
    expect(screen.queryByTestId("protected-content")).toBeNull();
  });

  it("never redirects when the guard is mounted on the login route itself", async () => {
    navigation.pathname = "/login";
    renderGuard(["admin"]);
    capturedAuthChangeCallback!(null);
    expect(await screen.findByText("403")).toBeTruthy();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it("transitions from spinner to children once auth resolves", async () => {
    renderGuard(["admin"]);
    expect(document.querySelector(".ant-spin")).toBeTruthy();
    capturedAuthChangeCallback!(authenticatedUser("admin"));
    await waitFor(() =>
      expect(document.querySelector(".ant-spin")).toBeFalsy()
    );
    expect(screen.getByTestId("protected-content")).toBeTruthy();
  });
});
