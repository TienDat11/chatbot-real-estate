// @vitest-environment jsdom
/**
 * Multi-device identity linking (2026-09-09): linkAnonIdentityAfterLogin sends
 * the device's persisted anon token (localStorage ANON_TOKEN_KEY) with a fresh
 * Firebase bearer to POST /api/auth/link-anon, and every failure path is
 * non-fatal (logged, swallowed, false).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ANON_TOKEN_KEY } from "@/features/chat/identity";
import {
  LINK_ANON_ENDPOINT,
  linkAnonIdentityAfterLogin,
} from "@/features/auth/linkAnonAfterLogin";

const getFreshIdTokenMock = vi.hoisted(() => vi.fn());

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  getFreshIdToken: getFreshIdTokenMock,
}));

function jsonResponse(status: number): Response {
  return new Response(null, { status });
}

const customer = { uid: "uid-customer", role: "viewer" as const };

describe("linkAnonIdentityAfterLogin", () => {
  beforeEach(() => {
    window.localStorage.clear();
    getFreshIdTokenMock.mockReset();
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("posts the stored anon token with a Firebase bearer", async () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_device_1");
    getFreshIdTokenMock.mockResolvedValue("id-token-1");
    const fetchMock = vi
      .spyOn(window, "fetch")
      .mockResolvedValue(jsonResponse(200));

    await expect(linkAnonIdentityAfterLogin(customer)).resolves.toBe(true);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe(LINK_ANON_ENDPOINT);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ anon_token: "tok_device_1" });
    expect(init.headers).toMatchObject({ Authorization: "Bearer id-token-1" });
  });

  it("is a no-op without a stored anon token", async () => {
    const fetchMock = vi.spyOn(window, "fetch").mockResolvedValue(jsonResponse(200));
    await expect(linkAnonIdentityAfterLogin(customer)).resolves.toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never links staff identities", async () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_device_1");
    const fetchMock = vi.spyOn(window, "fetch").mockResolvedValue(jsonResponse(200));
    for (const role of ["sales", "admin"] as const) {
      await expect(linkAnonIdentityAfterLogin({ uid: "u", role })).resolves.toBe(false);
    }
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getFreshIdTokenMock).not.toHaveBeenCalled();
  });

  it("treats 409 (rebind) and other non-2xx as non-fatal", async () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_device_1");
    getFreshIdTokenMock.mockResolvedValue("id-token-1");
    vi.spyOn(window, "fetch").mockResolvedValue(jsonResponse(409));
    await expect(linkAnonIdentityAfterLogin(customer)).resolves.toBe(false);
    vi.spyOn(window, "fetch").mockResolvedValue(jsonResponse(503));
    await expect(linkAnonIdentityAfterLogin(customer)).resolves.toBe(false);
  });

  it("swallows network failures and missing Firebase bearers", async () => {
    window.localStorage.setItem(ANON_TOKEN_KEY, "tok_device_1");
    vi.spyOn(window, "fetch").mockRejectedValue(new Error("offline"));
    await expect(linkAnonIdentityAfterLogin(customer)).resolves.toBe(false);
    getFreshIdTokenMock.mockRejectedValue(new Error("no user"));
    await expect(linkAnonIdentityAfterLogin(customer)).resolves.toBe(false);
  });
});
