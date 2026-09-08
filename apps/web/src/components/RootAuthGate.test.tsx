// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { AuthenticatedUser } from "@/infrastructure/firebase/firebaseAuthenticationService";
import type { Role } from "@/domain/auth/role";

const replaceMock = vi.fn();
let capturedAuthChangeCallback: ((user: AuthenticatedUser | null) => void) | null = null;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  signInWithEmail: vi.fn(),
  signUpWithEmail: vi.fn(),
  signOutUser: vi.fn(),
  onAuthChange: vi.fn((callback: (user: AuthenticatedUser | null) => void) => {
    capturedAuthChangeCallback = callback;
    return () => undefined;
  }),
}));

import { AuthProvider } from "@/lib/AuthProvider";
import { RootAuthGate } from "@/components/RootAuthGate";

function authenticatedUser(role: Role): AuthenticatedUser {
  return { uid: "u1", email: "a@b.vn", displayName: null, photoURL: null, role };
}

describe("RootAuthGate", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    capturedAuthChangeCallback = null;
  });

  afterEach(() => cleanup());

  it("waits for auth hydration before rendering login", () => {
    render(
      <AuthProvider>
        <RootAuthGate />
      </AuthProvider>,
    );

    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryByText("Đăng nhập hệ thống")).toBeNull();
  });

  it("redirects an authenticated sales user to the canonical sales home", async () => {
    render(
      <AuthProvider>
        <RootAuthGate />
      </AuthProvider>,
    );

    capturedAuthChangeCallback!(authenticatedUser("sales"));

    await vi.waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/sales/leads"));
    expect(screen.queryByText("Đăng nhập hệ thống")).toBeNull();
  });

  it("preserves the signed-out login and guest flow after hydration", async () => {
    render(
      <AuthProvider>
        <RootAuthGate />
      </AuthProvider>,
    );

    capturedAuthChangeCallback!(null);

    expect(await screen.findByText("Đăng nhập hệ thống")).toBeTruthy();
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("guest-entry")).toBeTruthy();
  });
});
