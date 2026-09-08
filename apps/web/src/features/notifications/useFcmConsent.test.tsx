// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, renderHook, waitFor } from "@testing-library/react";
import { useFcmConsent } from "@/features/notifications/useFcmConsent";

const registerFcmToken = vi.hoisted(() => vi.fn(async (_opts?: { requestPermission?: boolean }) => null as string | null));
const getFreshIdToken = vi.hoisted(() => vi.fn(async () => "bearer-token"));
const getAnonToken = vi.hoisted(() => vi.fn(() => null as string | null));vi.mock("@/infrastructure/firebase/firebaseMessaging", () => ({ registerFcmToken }));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({ getFreshIdToken }));
vi.mock("@/features/chat/identity", () => ({ getAnonToken }));

const CONSENT_KEY = "ragre.fcm.consent";
const TOKEN_KEY = "ragre.fcm.last_token";
const LAST_REGISTERED_AT_KEY = "ragre.fcm.last_registered_at";
// Consent/token are scoped by Firebase UID: `.<mode>.<uid>`.
const consentPath = (mode: string, uid: string) => `${CONSENT_KEY}.${mode}.${uid}`;
const tokenPath = (mode: string, uid: string) => `${TOKEN_KEY}.${mode}.${uid}`;
const lastRegPath = (mode: string, uid: string) => `${LAST_REGISTERED_AT_KEY}.${mode}.${uid}`;

function setPermission(value: NotificationPermission) {
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    value: { permission: value, requestPermission: vi.fn(async () => value) },
  });
}

// Stub navigator.permissions.query so the hook's Permissions API subscription
// path runs (jsdom has none by default). Returns the live PermissionStatus-like
// object so a test can fire status.onchange() to simulate an external revoke.
function stubPermissionsApi() {
  const status: { onchange: (() => void) | null } = { onchange: null };
  Object.defineProperty(navigator, "permissions", {
    configurable: true,
    value: { query: vi.fn(async () => status) },
  });
  return status;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  try {
    delete (navigator as unknown as { permissions?: unknown }).permissions;
  } catch {
    /* non-configurable in some jsdom builds */
  }
  window.localStorage.clear();
  // Neutral permission so a granted-permission restore never leaks across tests.
  setPermission("default");
  registerFcmToken.mockReset();
  registerFcmToken.mockImplementation(async (_opts?: { requestPermission?: boolean }) => null);
  getFreshIdToken.mockReset();
  getFreshIdToken.mockImplementation(async () => "bearer-token");
  getAnonToken.mockReset();
  getAnonToken.mockImplementation(() => null as string | null);
});

describe("useFcmConsent", () => {
  it("namespaces consent per mode AND uid so a sales toggle never enables client", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const sales = renderHook(() => useFcmConsent("sales", true, "uid-a"));
    await waitFor(() => expect(sales.result.current.enabled).toBe(false));
    await act(async () => { await sales.result.current.change(true); });
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("true");
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBeNull();
    // A freshly mounted client surface must not inherit the sales consent.
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(false));
  });

  it("SECURITY: legacy keys are never read into state — current uid stays disabled and legacy keys are removed", async () => {
    // Ownerless pre-UID values must NOT be inherited by whichever uid mounts
    // first after the upgrade (cross-account disclosure); they are deleted as
    // cleanup only, and a missing scoped key means disabled.
    window.localStorage.setItem(`${CONSENT_KEY}.sales`, "true");
    window.localStorage.setItem(CONSENT_KEY, "true");
    window.localStorage.setItem(`${TOKEN_KEY}.sales`, "tok-legacy");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const sales = renderHook(() => useFcmConsent("sales", true, "uid-a"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(sales.result.current.enabled).toBe(false);
    // Legacy keys removed unconditionally...
    expect(window.localStorage.getItem(`${CONSENT_KEY}.sales`)).toBeNull();
    expect(window.localStorage.getItem(CONSENT_KEY)).toBeNull();
    expect(window.localStorage.getItem(`${TOKEN_KEY}.sales`)).toBeNull();
    // ...and their values were never copied into the scoped namespace.
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBeNull();
    expect(window.localStorage.getItem(tokenPath("sales", "uid-a"))).toBeNull();
  });

  it("restores enabled from the scoped consent key for the current uid", async () => {
    window.localStorage.setItem(consentPath("sales", "uid-a"), "true");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const sales = renderHook(() => useFcmConsent("sales", true, "uid-a"));
    await waitFor(() => expect(sales.result.current.enabled).toBe(true));
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("true");
  });

  it("skips the DELETE request when disabling without a persisted token", async () => {
    window.localStorage.setItem(consentPath("sales", "uid-a"), "true");
    const fetchMock = vi.fn(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const sales = renderHook(() => useFcmConsent("sales", true, "uid-a"));
    await waitFor(() => expect(sales.result.current.enabled).toBe(true));
    await act(async () => { await sales.result.current.change(false); });
    // An empty-token DELETE would only produce backend 422 noise.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("false");
  });

  it("sends the DELETE with the stored token when disabling after registration", async () => {
    window.localStorage.setItem(consentPath("sales", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("sales", "uid-a"), "tok-1");
    const fetchMock = vi.fn(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const sales = renderHook(() => useFcmConsent("sales", true, "uid-a"));
    await waitFor(() => expect(sales.result.current.enabled).toBe(true));
    await act(async () => { await sales.result.current.change(false); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/notifications/device-token");
    expect(init.method).toBe("DELETE");
    // FastAPI rejects a JSON body sent without Content-Type as 422 — this
    // header is the regression guard for the observed consent-off failure.
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ token: "tok-1" });
    expect(window.localStorage.getItem(tokenPath("sales", "uid-a"))).toBeNull();
  });

  it("retries the client registration exactly once on 401 and then succeeds", async () => {
    registerFcmToken.mockResolvedValue("fcm-token-1");
    getAnonToken.mockReturnValueOnce(null).mockReturnValueOnce("anon-token");
    // First POST races the anon-token mint (no token -> 401); the retry
    // re-reads the token and succeeds.
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 401 } as Response)
      .mockResolvedValueOnce({ ok: true } as Response);
    vi.stubGlobal("fetch", fetchMock);
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), { timeout: 5000 });
    const [first, second] = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(first[1].method).toBe("POST");
    expect(second[1].method).toBe("POST");
    expect((second[1].headers as Record<string, string>)["X-Anon-Token"]).toBe("anon-token");
    expect((second[1].headers as Record<string, string>)["Authorization"]).toBeUndefined();
    expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBe("fcm-token-1");
  }, 10_000);

  it("does not retry the client registration on non-auth failures", async () => {
    registerFcmToken.mockResolvedValue("fcm-token-2");
    getAnonToken.mockReturnValueOnce("anon-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 } as Response);
    vi.stubGlobal("fetch", fetchMock);
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // A 500 is not the anon-token race: no retry loop, token stays unsaved.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBeNull();
  });

  it("restores enabled from granted permission + a registered token when the consent flag was lost", async () => {
    // Simulates a cleared/lost localStorage consent flag across sessions: the
    // browser permission (native, durable) plus a previously registered device
    // token are the reliable evidence that notifications were enabled, so a
    // fresh mount must report enabled, not OFF.
    setPermission("granted");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-persisted");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
  });

  it("stays disabled when permission is not granted even if a stale token exists", async () => {
    setPermission("default");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-stale");
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(client.result.current.enabled).toBe(false);
  });

  it("persists consent durably so a reload (fresh provider mount) reads enabled", async () => {
    setPermission("granted");
    registerFcmToken.mockResolvedValue("fcm-new");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const first = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(first.result.current.enabled).toBe(false));
    await act(async () => { await first.result.current.change(true); });
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("true");
    // Reload: a brand-new hook instance must restore the enabled state.
    const reloaded = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(reloaded.result.current.enabled).toBe(true));
  });

  // --- Security regressions: consent/token must be bound to the Firebase UID. ---

  // --- Single source of truth: the badge must reflect the native permission. ---

  it("reports nativePermission 'granted' and enabled when the browser granted and consent is stored", async () => {
    setPermission("granted");
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    expect(client.result.current.nativePermission).toBe("granted");
  });

  it("forces enabled false and nativePermission 'denied' even when a consent key exists", async () => {
    // The browser-level block wins over the in-app toggle: a stored consent must
    // never light the badge green while Notification.permission is 'denied'.
    setPermission("denied");
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(client.result.current.nativePermission).toBe("denied");
    expect(client.result.current.enabled).toBe(false);
  });

  it("change(true) requests permission when default and keeps consent enabled (not blocked)", async () => {
    setPermission("default");
    registerFcmToken.mockResolvedValue(null);
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(false));
    await act(async () => { await client.result.current.change(true); });
    // The toggle-on path is the one that prompts the browser for permission.
    expect(registerFcmToken).toHaveBeenCalledWith({ requestPermission: true });
    // 'default' is not a block, so the stored consent still reads as enabled.
    expect(client.result.current.nativePermission).toBe("default");
    expect(client.result.current.enabled).toBe(true);
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("true");
  });

  it("change(true) reverts consent and never persists enabled when the browser denies", async () => {
    setPermission("denied");
    registerFcmToken.mockResolvedValue(null);
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await act(async () => { await client.result.current.change(true); });
    expect(client.result.current.nativePermission).toBe("denied");
    expect(client.result.current.enabled).toBe(false);
    // The consent key must not be left 'true' contradicting the browser block.
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("false");
  });

  it("SECURITY: account B after logout/login does not inherit A's enabled state or token", async () => {
    registerFcmToken.mockResolvedValue("tok-A");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const view = renderHook(
      ({ active, uid }: { active: boolean; uid: string | null }) => useFcmConsent("sales", active, uid),
      { initialProps: { active: true, uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(false));
    await act(async () => { await view.result.current.change(true); });
    // A is enabled with its own scoped consent + registered token.
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("true");
    expect(window.localStorage.getItem(tokenPath("sales", "uid-a"))).toBe("tok-A");
    // Logout (uid -> null, active -> false), then account B logs in.
    view.rerender({ active: false, uid: null });
    view.rerender({ active: true, uid: "uid-b" });
    await waitFor(() => expect(view.result.current.enabled).toBe(false));
    // B must not see A's consent or reuse A's token.
    expect(window.localStorage.getItem(consentPath("sales", "uid-b"))).toBeNull();
    expect(window.localStorage.getItem(tokenPath("sales", "uid-b"))).toBeNull();
  });

  it("SECURITY: the restore effect re-runs on UID change so B starts from its own state", async () => {
    window.localStorage.setItem(consentPath("sales", "uid-a"), "true");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const view = renderHook(
      ({ uid }: { uid: string | null }) => useFcmConsent("sales", true, uid),
      { initialProps: { uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(true));
    // Switching to a different uid must re-run restore (the old (mode, active)
    // signature keyed only on mode, so a switch never re-ran it and leaked A).
    view.rerender({ uid: "uid-b" });
    await waitFor(() => expect(view.result.current.enabled).toBe(false));
  });

  it("SECURITY: an in-flight logout DELETE from A is cancelled when B logs in (epoch guard)", async () => {
    // A is enabled with a registered token so the logout path has a token to delete.
    window.localStorage.setItem(consentPath("sales", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("sales", "uid-a"), "tok-A");
    // The bearer mint resolves only after identity has moved on to B.
    let resolveBearer: (token: string) => void = () => {};
    getFreshIdToken.mockImplementationOnce(() => new Promise<string>((resolve) => { resolveBearer = resolve; }));
    const fetchMock = vi.fn(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(
      ({ active, uid }: { active: boolean; uid: string | null }) => useFcmConsent("sales", active, uid),
      { initialProps: { active: true, uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(true));
    // Logout A: schedules the DELETE, which now awaits the (held) bearer.
    view.rerender({ active: false, uid: null });
    // B logs in before the bearer resolves: the identity epoch moves on.
    view.rerender({ active: true, uid: "uid-b" });
    await act(async () => { resolveBearer("bearer-b"); await Promise.resolve(); });
    // The stale A logout DELETE must NOT fire — it would target B's session.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // --- Reviewer fixes: external revocation, error-safe revert, SSR determinism. ---

  it("external revoke via focus/visibility refresh flips the badge to denied and normalizes persisted state (Permissions API unavailable)", async () => {
    // jsdom has no Permissions API, so navigator.permissions is undefined and the
    // instant onchange path never runs — the visibility/focus fallback is the
    // only way an OS/browser-settings revoke reaches the badge.
    setPermission("granted");
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-live");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    expect(client.result.current.nativePermission).toBe("granted");
    // The user revokes notifications in browser/OS settings while the tab is open.
    setPermission("denied");
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    // Badge flips to denied/disabled and persisted consent + token agree.
    await waitFor(() => expect(client.result.current.nativePermission).toBe("denied"));
    expect(client.result.current.enabled).toBe(false);
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("false");
    await waitFor(() => expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBeNull());
    const del = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(del).toBeTruthy();
    expect(JSON.parse(String(del![1]?.body))).toEqual({ token: "tok-live" });
  });

  it("change(true) still normalizes consent to false when token registration rejects and the browser denied", async () => {
    // Regression: previously a rejected registerFcmToken was swallowed by the
    // catch and the permission re-read + revert were skipped, leaving a stale
    // persisted "true" whenever the Permissions API could not correct it.
    setPermission("default");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(false));
    // The prompt results in a denial and registration throws.
    setPermission("denied");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-x");
    registerFcmToken.mockRejectedValueOnce(new Error("registration failed"));
    await act(async () => { await client.result.current.change(true); });
    expect(client.result.current.nativePermission).toBe("denied");
    expect(client.result.current.enabled).toBe(false);
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("false");
    await waitFor(() => expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBeNull());
    const del = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(del).toBeTruthy();
    expect(JSON.parse(String(del![1]?.body))).toEqual({ token: "tok-x" });
  });

  it("SSR: nativePermission initializes to a deterministic 'default' then updates after the mount effect", async () => {
    // The server renders "default"; the browser's real permission is read in an
    // effect, so the first committed render must NOT already reflect the browser
    // state (otherwise the hydration HTML and first client paint disagree).
    setPermission("granted");
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true }) as Response));
    const seen: NotificationPermission[] = [];
    function Probe() {
      const { nativePermission } = useFcmConsent("client", true, "uid-a");
      seen.push(nativePermission);
      return null;
    }
    render(<Probe />);
    // First render uses the deterministic initializer, not Notification.permission.
    expect(seen[0]).toBe("default");
    // After the mount effect reads the real permission, a later render reflects it.
    await waitFor(() => expect(seen).toContain("granted"));
  });

  // --- Reviewer fixes: UID/epoch-scoped transition guard + single-flight DELETE. ---

  it("UID switch while denied: B's consent normalizes to false and B's token is dropped, A untouched", async () => {
    // A enables with its own scoped consent + registered token.
    setPermission("granted");
    registerFcmToken.mockResolvedValue("tok-A");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(
      ({ uid }: { uid: string | null }) => useFcmConsent("sales", true, uid),
      { initialProps: { uid: "uid-a" as string | null } },
    );
    await act(async () => { await view.result.current.change(true); });
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("true");
    expect(window.localStorage.getItem(tokenPath("sales", "uid-a"))).toBe("tok-A");
    fetchMock.mockClear();
    // Switch to B, whose browser permission is denied and which has a stored token.
    setPermission("denied");
    window.localStorage.setItem(consentPath("sales", "uid-b"), "true");
    window.localStorage.setItem(tokenPath("sales", "uid-b"), "tok-B");
    view.rerender({ uid: "uid-b" });
    // B's persisted consent is normalized to false and its token removed (+DELETE).
    await waitFor(() => expect(window.localStorage.getItem(consentPath("sales", "uid-b"))).toBe("false"));
    await waitFor(() => expect(window.localStorage.getItem(tokenPath("sales", "uid-b"))).toBeNull());
    expect(view.result.current.enabled).toBe(false);
    expect(view.result.current.nativePermission).toBe("denied");
    const delB = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(delB).toBeTruthy();
    expect(JSON.parse(String(delB![1]?.body))).toEqual({ token: "tok-B" });
    // A's scoped keys must be untouched by B's normalization.
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("true");
    expect(window.localStorage.getItem(tokenPath("sales", "uid-a"))).toBe("tok-A");
  });

  it("stale cancelled change() for A does not poison B's ref/state or fire a stray DELETE", async () => {
    // A is enabled with a stored token so the denial path has something to delete.
    setPermission("granted");
    window.localStorage.setItem(consentPath("sales", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("sales", "uid-a"), "tok-A");
    // Hold the bearer so the DELETE inside unregisterStoredToken stays in flight
    // across the identity switch, proving the epoch guard cancels it.
    let resolveBearer: (token: string) => void = () => {};
    getFreshIdToken.mockImplementationOnce(() => new Promise<string>((resolve) => { resolveBearer = resolve; }));
    const fetchMock = vi.fn(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(
      ({ uid }: { uid: string | null }) => useFcmConsent("sales", true, uid),
      { initialProps: { uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(true));
    // A's toggle-on prompt is denied; the DELETE awaits the (held) bearer, so the
    // change is still in flight when the identity switches to B.
    setPermission("denied");
    let changePromise: Promise<void> | undefined;
    await act(async () => { changePromise = view.result.current.change(true); });
    expect(view.result.current.nativePermission).toBe("denied");
    // Switch to B (permission granted, its own stored consent) before A's
    // change resolves.
    setPermission("granted");
    window.localStorage.setItem(consentPath("sales", "uid-b"), "true");
    await act(async () => { view.rerender({ uid: "uid-b" }); });
    // Let A's in-flight change finish after the switch.
    await act(async () => { resolveBearer("bearer-b"); await changePromise; });
    // B's real permission wins; A's stale "denied" must not leak into B's state.
    expect(view.result.current.nativePermission).toBe("granted");
    expect(view.result.current.enabled).toBe(true);
    // The cancelled A DELETE never fires after the switch.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exactly one DELETE when PermissionStatus.onchange and change() both observe a denial", async () => {
    // Both the Permissions API onchange path and change()'s unconditional denial
    // branch can fire on a prompt denial; the removeItem-first guard collapses
    // them into a single backend DELETE.
    setPermission("default");
    const status = stubPermissionsApi();
    registerFcmToken.mockImplementation(async () => {
      // The browser prompt resolves as a denial.
      setPermission("denied");
      status.onchange?.();
      return null;
    });
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-dup");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(false));
    await act(async () => { await client.result.current.change(true); });
    const dels = fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE");
    expect(dels).toHaveLength(1);
    expect(JSON.parse(String(dels[0][1]?.body))).toEqual({ token: "tok-dup" });
    expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBeNull();
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("false");
    expect(client.result.current.nativePermission).toBe("denied");
    expect(client.result.current.enabled).toBe(false);
  });

  it("Permissions API onchange path flips nativePermission to denied and normalizes consent", async () => {
    setPermission("granted");
    const status = stubPermissionsApi();
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-live");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    expect(client.result.current.nativePermission).toBe("granted");
    // External revoke: permission flips to denied and the status fires onchange.
    setPermission("denied");
    await act(async () => { status.onchange?.(); });
    await waitFor(() => expect(client.result.current.nativePermission).toBe("denied"));
    expect(client.result.current.enabled).toBe(false);
    expect(window.localStorage.getItem(consentPath("client", "uid-a"))).toBe("false");
    await waitFor(() => expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBeNull());
    const del = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(del).toBeTruthy();
    expect(JSON.parse(String(del![1]?.body))).toEqual({ token: "tok-live" });
  });

  it("SECURITY: a UID switch never registers a token for B while B's restore is pending (stale enabled guard)", async () => {
    // A enables with its own scoped consent + registered token.
    registerFcmToken.mockResolvedValue("tok-A");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(
      ({ active, uid }: { active: boolean; uid: string | null }) => useFcmConsent("sales", active, uid),
      { initialProps: { active: true, uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(false));
    await act(async () => { await view.result.current.change(true); });
    expect(window.localStorage.getItem(consentPath("sales", "uid-a"))).toBe("true");
    expect(window.localStorage.getItem(tokenPath("sales", "uid-a"))).toBe("tok-A");
    fetchMock.mockClear();
    registerFcmToken.mockClear();
    // Switch to B (active, no consent stored for B). A's stale `enabled` must
    // NOT carry into B's identity: the sync effect is gated until B's restore
    // completes, so no token is registered and no POST is issued for B.
    view.rerender({ active: true, uid: "uid-b" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(tokenPath("sales", "uid-b"))).toBeNull();
    expect(window.localStorage.getItem(consentPath("sales", "uid-b"))).toBeNull();
    expect(view.result.current.enabled).toBe(false);
  });

  it("SECURITY: a stale change(true) for A is cancelled before any device-token POST under B", async () => {
    // Only the toggle-on prompt path returns a token, so the background sync
    // effect (no requestPermission) stays inert and the sole POST candidate is
    // the in-flight change() under test.
    registerFcmToken.mockImplementation(async (opts?: { requestPermission?: boolean }) =>
      opts?.requestPermission ? "tok-A" : null);
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(
      ({ uid }: { uid: string | null }) => useFcmConsent("sales", true, uid),
      { initialProps: { uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(false));
    // Start A's toggle-on; the POST inside registerDeviceToken awaits the
    // (held) bearer, so the change is still in flight across the switch.
    let changePromise: Promise<void> | undefined;
    let resolveBearer: (token: string) => void = () => {};
    getFreshIdToken.mockImplementationOnce(() => new Promise<string>((resolve) => { resolveBearer = resolve; }));
    await act(async () => { changePromise = view.result.current.change(true); });
    expect(fetchMock).not.toHaveBeenCalled();
    // B logs in before A's in-flight POST resolves: the epoch moves on.
    view.rerender({ uid: "uid-b" });
    await act(async () => { resolveBearer("bearer-b"); await changePromise; });
    // The stale A registration must NOT POST under B's identity.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(tokenPath("sales", "uid-b"))).toBeNull();
  });

  it("SECURITY: registerDeviceToken issues no POST once the epoch has moved (cancellation before fetch)", async () => {
    // Directly covers the registerDeviceToken pre-POST cancellation via the
    // public change() path: the bearer resolves only after the identity has
    // switched, so the POST must be dropped and no fetch call made.
    registerFcmToken.mockImplementation(async (opts?: { requestPermission?: boolean }) =>
      opts?.requestPermission ? "tok-A" : null);
    let resolveBearer: (token: string) => void = () => {};
    getFreshIdToken.mockImplementationOnce(() => new Promise<string>((resolve) => { resolveBearer = resolve; }));
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(
      ({ uid }: { uid: string | null }) => useFcmConsent("sales", true, uid),
      { initialProps: { uid: "uid-a" as string | null } },
    );
    await waitFor(() => expect(view.result.current.enabled).toBe(false));
    let changePromise: Promise<void> | undefined;
    await act(async () => { changePromise = view.result.current.change(true); });
    view.rerender({ uid: "uid-b" });
    await act(async () => { resolveBearer("bearer-b"); await changePromise; });
    // The device-token route was never hit for the stale identity.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // --- Registration freshness heal: an unchanged token must not mask a
  //     backend row that was pruned/disabled server-side. ---

  it("re-POSTs an unchanged token when the registration timestamp is missing (heal a pruned row)", async () => {
    setPermission("granted");
    registerFcmToken.mockResolvedValue("tok-same");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    // Token already matches localStorage but no timestamp exists (e.g. the row
    // was pruned after a prior session, or the key was evicted).
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-same");
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/notifications/device-token");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ token: "tok-same", platform: "web" });
    // A successful re-registration stamps the freshness clock.
    expect(Number(window.localStorage.getItem(lastRegPath("client", "uid-a")))).toBeGreaterThan(0);
  });

  it("re-POSTs an unchanged token when the stored timestamp is older than the refresh window", async () => {
    setPermission("granted");
    registerFcmToken.mockResolvedValue("tok-same");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const eightDaysMs = 8 * 24 * 60 * 60 * 1000;
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-same");
    window.localStorage.setItem(lastRegPath("client", "uid-a"), String(Date.now() - eightDaysMs));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].method).toBe("POST");
    // The refreshed timestamp is now within the window.
    expect(Date.now() - Number(window.localStorage.getItem(lastRegPath("client", "uid-a")))).toBeLessThan(60_000);
  });

  it("skips the POST when the token is unchanged and the timestamp is fresh", async () => {
    setPermission("granted");
    registerFcmToken.mockResolvedValue("tok-same");
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-same");
    window.localStorage.setItem(lastRegPath("client", "uid-a"), String(Date.now()));
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 50));
    // Fresh registration: no redundant POST for an unchanged, live token.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("clears the registration timestamp when consent is turned off", async () => {
    setPermission("granted");
    window.localStorage.setItem(consentPath("client", "uid-a"), "true");
    window.localStorage.setItem(tokenPath("client", "uid-a"), "tok-x");
    window.localStorage.setItem(lastRegPath("client", "uid-a"), String(Date.now()));
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => ({ ok: true }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    const client = renderHook(() => useFcmConsent("client", true, "uid-a"));
    await waitFor(() => expect(client.result.current.enabled).toBe(true));
    await act(async () => { await client.result.current.change(false); });
    expect(window.localStorage.getItem(tokenPath("client", "uid-a"))).toBeNull();
    expect(window.localStorage.getItem(lastRegPath("client", "uid-a"))).toBeNull();
  });
});
