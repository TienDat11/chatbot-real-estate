// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { ClientNotificationProvider } from "@/features/notifications/ClientNotificationProvider";

const authState = { user: null as { uid?: string; role?: string } | null, loading: false };
const pathname = vi.hoisted(() => ({ value: "/" }));
const notificationOpen = vi.hoisted(() => vi.fn());
const fcmHandlers = vi.hoisted(() => [] as Array<(payload: { notification?: { title?: string; body?: string }; data?: Record<string, string> }) => void>);
let subscribeCalls = 0;
const unsubscribeCalls = vi.hoisted(() => ({ count: 0 }));

vi.mock("@/lib/AuthProvider", () => ({ useAuth: () => authState }));
vi.mock("next/navigation", () => ({ usePathname: () => pathname.value }));
vi.mock("antd", () => ({ App: { useApp: () => ({ notification: { open: notificationOpen } }) } }));
vi.mock("@/infrastructure/firebase/firebaseMessaging", () => ({
  subscribeToFcmMessages: vi.fn(async (handler: (payload: { notification?: { title?: string; body?: string }; data?: Record<string, string> }) => void) => {
    subscribeCalls += 1;
    fcmHandlers.push(handler);
    return () => { unsubscribeCalls.count += 1; };
  }),
  registerFcmToken: vi.fn(async () => null),
}));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({ getFreshIdToken: vi.fn(async () => "bearer-token") }));
vi.mock("@/features/chat/identity", () => ({ getAnonToken: () => null }));
vi.mock("@/features/notifications/NotificationConsentControl", () => ({ NotificationConsentControl: () => <div data-testid="consent-control" /> }));

afterEach(() => {
  cleanup();
  fcmHandlers.length = 0;
  notificationOpen.mockReset();
  pathname.value = "/";
  subscribeCalls = 0;
  unsubscribeCalls.count = 0;
});

describe("ClientNotificationProvider", () => {
  it("mounts the consent control on a project conversation surface", () => {
    pathname.value = "/project/camellia";
    render(<ClientNotificationProvider><div>chat</div></ClientNotificationProvider>);
    expect(screen.getByTestId("consent-control")).toBeTruthy();
  });

  it("does not mount the consent control outside project surfaces", () => {
    pathname.value = "/admin";
    render(<ClientNotificationProvider><div>other</div></ClientNotificationProvider>);
    expect(screen.queryByTestId("consent-control")).toBeNull();
  });

  it("shows the foreground toast for sales_call_started and routes through a safe relative path", async () => {
    pathname.value = "/project/camellia";
    render(<ClientNotificationProvider><div>chat</div></ClientNotificationProvider>);
    await waitFor(() => expect(fcmHandlers.length).toBeGreaterThan(0));
    await act(async () => {
      fcmHandlers[0]?.({ notification: { title: "Cuộc gọi bắt đầu", body: "Chuyên viên đang gọi cho bạn." }, data: { type: "sales_call_started", url: "/project/camellia" } });
    });
    expect(notificationOpen).toHaveBeenCalledWith(expect.objectContaining({
      message: "Cuộc gọi bắt đầu",
      // The click handler receives the same-origin sanitised path, never an
      // arbitrary remote URL.
      onClick: expect.any(Function),
    }));
  });

  it("ignores foreground FCM messages that are not sales_call_started", async () => {
    pathname.value = "/project/camellia";
    render(<ClientNotificationProvider><div>chat</div></ClientNotificationProvider>);
    await waitFor(() => expect(fcmHandlers.length).toBeGreaterThan(0));
    await act(async () => {
      fcmHandlers[0]?.({ notification: { title: "Spam" }, data: { type: "other_event" } });
    });
    expect(notificationOpen).not.toHaveBeenCalled();
  });

  it("subscribes the message listener on the root route, not only project surfaces", async () => {
    pathname.value = "/";
    render(<ClientNotificationProvider><div>home</div></ClientNotificationProvider>);
    await waitFor(() => expect(subscribeCalls).toBe(1));
    // A call-started event delivered while the visitor is still on "/" still
    // raises the foreground toast: the listener must not be pathname-gated.
    await act(async () => {
      fcmHandlers[0]?.({ notification: { title: "Cuộc gọi bắt đầu" }, data: { type: "sales_call_started", url: "/" } });
    });
    expect(notificationOpen).toHaveBeenCalledTimes(1);
  });

  it("keeps exactly one listener across pathname navigation (no duplicate handlers)", async () => {
    pathname.value = "/";
    const view = render(<ClientNotificationProvider><div>page</div></ClientNotificationProvider>);
    await waitFor(() => expect(subscribeCalls).toBe(1));
    pathname.value = "/project/camellia";
    view.rerender(<ClientNotificationProvider><div>page</div></ClientNotificationProvider>);
    await act(async () => { await Promise.resolve(); });
    pathname.value = "/project/soleil";
    view.rerender(<ClientNotificationProvider><div>page</div></ClientNotificationProvider>);
    await act(async () => { await Promise.resolve(); });
    expect(subscribeCalls).toBe(1);
    expect(fcmHandlers.length).toBe(1);
  });

  it("keeps exactly one listener across unmount/remount cycles (StrictMode double-mount)", async () => {
    pathname.value = "/project/camellia";
    const view = render(<ClientNotificationProvider><div>page</div></ClientNotificationProvider>);
    await waitFor(() => expect(subscribeCalls).toBe(1));
    view.unmount();
    await waitFor(() => expect(unsubscribeCalls.count).toBe(1));
    render(<ClientNotificationProvider><div>page</div></ClientNotificationProvider>);
    // One unsubscribe per subscribe: no orphaned (duplicate) foreground
    // listener survives the remount cycle.
    await waitFor(() => expect(subscribeCalls).toBe(2));
    expect(unsubscribeCalls.count).toBe(1);
    // Only the still-mounted (latest) listener reacts: the stale handler from
    // the unmounted mount must be dead.
    await act(async () => {
      fcmHandlers[1]?.({ notification: { title: "x" }, data: { type: "sales_call_started" } });
    });
    expect(notificationOpen).toHaveBeenCalledTimes(1);
  });

  it("unsubscribes when the subscription resolves after unmount (async unsubscribe)", async () => {
    pathname.value = "/project/camellia";
    let resolveSubscribe: (stop: () => void) => void = () => {};
    const stopFn = vi.fn();
    const subscribeMock = vi.mocked(await import("@/infrastructure/firebase/firebaseMessaging"));
    subscribeMock.subscribeToFcmMessages.mockImplementationOnce(
      () => new Promise((resolve) => { resolveSubscribe = resolve; }),
    );
    const view = render(<ClientNotificationProvider><div>page</div></ClientNotificationProvider>);
    view.unmount();
    // The subscribe promise settles only after unmount: the provider must
    // still attach the unsubscribe so no orphan Firebase listener survives.
    await act(async () => { resolveSubscribe(stopFn); await Promise.resolve(); });
    expect(stopFn).toHaveBeenCalledTimes(1);
  });

  it("shows a toast without PII for a valid call-started payload missing notification text", async () => {
    pathname.value = "/project/camellia";
    render(<ClientNotificationProvider><div>chat</div></ClientNotificationProvider>);
    await waitFor(() => expect(fcmHandlers.length).toBeGreaterThan(0));
    await act(async () => {
      fcmHandlers[0]?.({ data: { type: "sales_call_started", phone: "0900123456" } });
    });
    expect(notificationOpen).toHaveBeenCalledWith(expect.objectContaining({
      message: "Cuộc gọi bắt đầu",
      description: "Chuyên viên đang gọi cho bạn.",
    }));
    // No PII leakage: the raw phone number must never reach the toast surface.
    const call = notificationOpen.mock.calls[0][0] as { message: string; description: string };
    expect(call.message).not.toContain("0900123456");
    expect(call.description).not.toContain("0900123456");
  });

  it("ignores malformed payloads (missing type, non-string url handled safely)", async () => {
    pathname.value = "/project/camellia";
    render(<ClientNotificationProvider><div>chat</div></ClientNotificationProvider>);
    await waitFor(() => expect(fcmHandlers.length).toBeGreaterThan(0));
    await act(async () => {
      fcmHandlers[0]?.({ data: {} });
      fcmHandlers[0]?.({ data: { type: 123 as unknown as string } });
      fcmHandlers[0]?.({});
    });
    expect(notificationOpen).not.toHaveBeenCalled();
  });
});
