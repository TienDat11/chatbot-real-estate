// @vitest-environment jsdom
// Foreground-delivery regression: the FCM service worker is the channel that
// carries a push to an open tab, so subscribeToFcmMessages must register it
// whenever browser permission is already granted — independent of the consent
// toggle and of the backend device-token POST succeeding.
import { afterEach, describe, expect, it, vi } from "vitest";

const register = vi.hoisted(() => vi.fn(async (_url: string) => ({ scope: "/" }) as unknown as ServiceWorkerRegistration));
const onMessage = vi.hoisted(() => vi.fn(() => () => undefined));
const isSupported = vi.hoisted(() => vi.fn(async () => true));
const getToken = vi.hoisted(() => vi.fn(async () => "fcm-token"));
const fakeMessaging = { id: "messaging" } as unknown;

vi.mock("firebase/messaging", () => ({
  isSupported,
  onMessage,
  getToken,
}));
vi.mock("@/infrastructure/firebase/firebaseMessagingClient", () => ({
  getFirebaseMessaging: vi.fn(async () => fakeMessaging),
}));
vi.mock("@/infrastructure/firebase/firebaseEnvironmentConfig", () => ({
  readFirebaseEnvironmentConfig: () => ({
    apiKey: "k",
    authDomain: "a",
    projectId: "p",
    storageBucket: "s",
    messagingSenderId: "ms",
    appId: "ai",
  }),
}));

function setPermission(value: NotificationPermission) {
  Object.defineProperty(globalThis, "Notification", {
    configurable: true,
    value: { permission: value, requestPermission: vi.fn(async () => value) },
  });
}

function installServiceWorker() {
  Object.defineProperty(globalThis.navigator, "serviceWorker", {
    configurable: true,
    writable: true,
    value: { register },
  });
}

afterEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

describe("subscribeToFcmMessages foreground service-worker bootstrap", () => {
  it("registers the FCM service worker when permission is already granted, even with no consent/token POST", async () => {
    installServiceWorker();
    setPermission("granted");
    const { subscribeToFcmMessages } = await import("@/infrastructure/firebase/firebaseMessaging");
    const unsubscribe = await subscribeToFcmMessages(() => undefined);
    // The channel must be established before onMessage attaches, so a push
    // arriving on this surface is actually delivered to the tab.
    expect(register).toHaveBeenCalledTimes(1);
    expect(String(register.mock.calls[0][0])).toContain("/firebase-messaging-sw.js");
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(typeof unsubscribe).toBe("function");
  });

  it("does not register the service worker when permission is not granted", async () => {
    installServiceWorker();
    setPermission("default");
    const { subscribeToFcmMessages } = await import("@/infrastructure/firebase/firebaseMessaging");
    await subscribeToFcmMessages(() => undefined);
    // No silent registration/prompt when the user has not granted permission.
    expect(register).not.toHaveBeenCalled();
    expect(onMessage).toHaveBeenCalledTimes(1);
  });

  it("reuses a single service-worker registration across subscriptions", async () => {
    installServiceWorker();
    setPermission("granted");
    const { subscribeToFcmMessages } = await import("@/infrastructure/firebase/firebaseMessaging");
    await subscribeToFcmMessages(() => undefined);
    await subscribeToFcmMessages(() => undefined);
    // Two providers (client + sales) mount the listener; the SW registers once.
    expect(register).toHaveBeenCalledTimes(1);
    expect(onMessage).toHaveBeenCalledTimes(2);
  });
});

describe("registerFcmToken", () => {
  it("returns null without prompting when permission is not granted", async () => {
    installServiceWorker();
    setPermission("default");
    const { registerFcmToken } = await import("@/infrastructure/firebase/firebaseMessaging");
    expect(await registerFcmToken()).toBeNull();
    expect(register).not.toHaveBeenCalled();
    expect(getToken).not.toHaveBeenCalled();
  });

  it("registers the service worker and returns a token when permission is granted", async () => {
    installServiceWorker();
    setPermission("granted");
    const { registerFcmToken } = await import("@/infrastructure/firebase/firebaseMessaging");
    const token = await registerFcmToken();
    expect(token).toBe("fcm-token");
    expect(register).toHaveBeenCalledTimes(1);
    expect(getToken).toHaveBeenCalledTimes(1);
  });
});

describe("fcm registration diagnostics (console-only, no UI)", () => {
  it("warns when the service worker registration fails", async () => {
    installServiceWorker();
    setPermission("granted");
    register.mockRejectedValueOnce(new Error("sw boom"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { ensureFcmServiceWorkerRegistration } = await import("@/infrastructure/firebase/firebaseMessaging");
    expect(await ensureFcmServiceWorkerRegistration()).toBeNull();
    expect(warn).toHaveBeenCalledWith("[fcm] service worker registration failed", expect.any(Error));
    warn.mockRestore();
  });

  it("returns null and warns when getToken fails", async () => {
    installServiceWorker();
    setPermission("granted");
    getToken.mockRejectedValueOnce(new Error("token boom"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { registerFcmToken } = await import("@/infrastructure/firebase/firebaseMessaging");
    expect(await registerFcmToken()).toBeNull();
    expect(warn).toHaveBeenCalledWith("[fcm] getToken failed", expect.any(Error));
    warn.mockRestore();
  });
});
