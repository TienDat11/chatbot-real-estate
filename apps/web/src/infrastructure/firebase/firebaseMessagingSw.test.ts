// Regression tests for the real service-worker script: the file is evaluated
// in a sandboxed VM context with only the ServiceWorkerGlobals the script
// actually touches (importScripts, firebase, self, clients, URL). This
// exercises the actual published code, not a parallel reimplementation.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { describe, expect, it, vi, beforeEach } from "vitest";

const SW_PATH = new URL("../../../public/firebase-messaging-sw.js", import.meta.url);

interface SwSandbox {
  backgroundHandler: ((payload: { notification?: { title?: string; body?: string }; data?: Record<string, string> }) => void) | null;
  clickHandler: ((event: {
    notification: { close: () => void; data?: Record<string, string> };
    waitUntil: (promise: Promise<unknown>) => void;
  }) => void) | null;
  showNotification: ReturnType<typeof vi.fn>;
  navigate: ReturnType<typeof vi.fn>;
  focus: ReturnType<typeof vi.fn>;
  openWindow: ReturnType<typeof vi.fn>;
}

function loadServiceWorker(): SwSandbox {
  const source = readFileSync(SW_PATH, "utf8");
  const sandbox: SwSandbox = {
    backgroundHandler: null,
    clickHandler: null,
    showNotification: vi.fn(),
    navigate: vi.fn(async () => undefined),
    focus: vi.fn(async () => undefined),
    openWindow: vi.fn(async () => undefined),
  };
  const context = {
    URL,
    importScripts: vi.fn(),
    firebase: {
      initializeApp: vi.fn(),
      messaging: () => ({ onBackgroundMessage: (handler: SwSandbox["backgroundHandler"]) => { sandbox.backgroundHandler = handler ?? null; } }),
    },
    self: {
      location: { href: "https://app.example/firebase-messaging-sw.js?apiKey=k&appId=a", origin: "https://app.example" },
      registration: null as unknown,
      addEventListener: (type: string, handler: (event: never) => void) => { if (type === "notificationclick") sandbox.clickHandler = handler as SwSandbox["clickHandler"]; },
    },
    clients: {
      matchAll: async () => [{ url: "https://app.example/project/camellia", focus: sandbox.focus, navigate: sandbox.navigate }],
      openWindow: sandbox.openWindow,
    },
  };
  context.self.registration = { showNotification: sandbox.showNotification };
  vm.runInNewContext(source, context, { filename: "firebase-messaging-sw.js" });
  return sandbox;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("firebase-messaging-sw.js background delivery", () => {
  it("shows a manual notification for data-only messages", () => {
    const sw = loadServiceWorker();
    expect(sw.backgroundHandler).toBeTypeOf("function");
    sw.backgroundHandler?.({ data: { type: "new_lead", lead_id: "9" } });
    expect(sw.showNotification).toHaveBeenCalledTimes(1);
    expect(sw.showNotification.mock.calls[0][1]).toMatchObject({ data: { type: "new_lead", lead_id: "9" } });
  });

  it("does not double-display notification+data messages (SDK auto-display owns them)", () => {
    const sw = loadServiceWorker();
    sw.backgroundHandler?.({ notification: { title: "Lead mới", body: "Chi nhánh Q7" }, data: { type: "new_lead" } });
    // The FCM SDK already displays messages carrying a notification payload;
    // a manual showNotification here would produce a duplicate toast.
    expect(sw.showNotification).not.toHaveBeenCalled();
  });

  it("opens a safe same-origin relative path and reuses an existing client on click", async () => {
    const sw = loadServiceWorker();
    expect(sw.clickHandler).toBeTypeOf("function");
    let settled = false;
    const promise = Promise.resolve();
    sw.clickHandler?.({
      notification: { close: () => undefined, data: { url: "/project/camellia?x=1#lead" } },
      waitUntil: (p) => { void p.then(() => { settled = true; }); },
    });
    await vi.waitFor(() => expect(settled).toBe(true));
    expect(sw.navigate).toHaveBeenCalledWith("/project/camellia?x=1#lead");
    expect(sw.focus).toHaveBeenCalledTimes(1);
    expect(sw.openWindow).not.toHaveBeenCalled();
  });

  it("falls back to the root path for cross-origin or non-relative click urls", async () => {
    const sw = loadServiceWorker();
    let settled = false;
    sw.clickHandler?.({
      notification: { close: () => undefined, data: { url: "https://evil.example/phish" } },
      waitUntil: (p) => { void p.then(() => { settled = true; }); },
    });
    await vi.waitFor(() => expect(settled).toBe(true));
    // The malicious URL never reaches any navigation surface; the existing
    // client is simply reused (focused) instead.
    expect(sw.navigate).not.toHaveBeenCalled();
    expect(sw.navigate).not.toHaveBeenCalledWith("https://evil.example/phish");
    expect(sw.focus).toHaveBeenCalledTimes(1);
    expect(sw.openWindow).not.toHaveBeenCalled();
  });
});
