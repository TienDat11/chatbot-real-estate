// @vitest-environment jsdom
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { SalesNotificationProvider, useSalesNotifications } from "./SalesNotificationProvider";

const authState = { user: { uid: "uid-a", role: "sales" as const }, loading: false };
const streams: Array<{ onLeadsChanged: (leads: unknown[]) => void }> = [];
const requests: Array<{ resolve: (value: Response) => void; reject: (error: Error) => void }> = [];
const notificationOpen = vi.hoisted(() => vi.fn());
type FcmPayload = { notification?: { title?: string; body?: string }; data?: Record<string, string> };
type FcmHandler = (payload: FcmPayload) => void;
const fcmHandlers = vi.hoisted(() => [] as FcmHandler[]);
vi.mock("@/lib/AuthProvider", () => ({ useAuth: () => authState }));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({ getFreshIdToken: vi.fn(async () => "token") }));
vi.mock("@/features/crm/useCrmLeadStream", () => ({ CrmLeadStreamProvider: ({ options, children }: { options: { onIncomingLead?: (lead: unknown) => void }; children: React.ReactNode }) => { streams.push({ onLeadsChanged: (leads) => { options.onIncomingLead?.(leads[leads.length - 1]); } }); return children; } }));
vi.mock("@/infrastructure/firebase/firebaseMessaging", () => ({ registerFcmToken: vi.fn(async () => null), subscribeToFcmMessages: vi.fn(async (handler: (payload: { notification?: { title?: string }; data?: Record<string, string> }) => void) => { fcmHandlers.push(handler); return () => undefined; }) }));
vi.mock("antd", () => ({ App: { useApp: () => ({ notification: { open: notificationOpen } }) } }));

function Consumer() {
  const state = useSalesNotifications();
  return <div><output data-testid="count">{state?.unreadCount}</output><output data-testid="error">{state?.error ?? ""}</output><output data-testid="items">{state?.items.map((item) => item.id).join(",")}</output><button onClick={() => { const target = state?.items.find((item) => !item.read_at); if (target) void state?.markRead(target.id); }}>read</button><button onClick={() => void state?.markAllRead()}>read-all</button></div>;
}
function response(body: unknown) { return { ok: true, json: async () => body } as Response; }
function failure(status: number) { return { ok: false, status, json: async () => ({ detail: "rejected" }) } as Response; }
function lead(id: string) { return { id, leadId: id === "n1" ? "1" : "2", projectKey: "camellia", maskedPhone: null, name: id, createdAt: "2026-01-01T00:00:00Z" }; }
function renderProvider() { return render(<SalesNotificationProvider><Consumer /></SalesNotificationProvider>); }

afterEach(() => { cleanup(); vi.restoreAllMocks(); notificationOpen.mockReset(); requests.length = 0; streams.length = 0; fcmHandlers.length = 0; authState.user = { uid: "uid-a", role: "sales" }; });

describe("SalesNotificationProvider", () => {
  it("isolates items and count when the auth UID changes", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve, reject) => requests.push({ resolve, reject }))));
    const view = renderProvider();
    await screen.findByTestId("count");
    await act(async () => requests.shift()?.resolve(response({ items: [{ id: "old", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 1 })));
    expect(await screen.findByText("old")).toBeTruthy();
    authState.user = { uid: "uid-b", role: "sales" };
    view.rerender(<SalesNotificationProvider><Consumer /></SalesNotificationProvider>);
    expect(screen.getByTestId("items").textContent).toBe("");
    expect(screen.getByTestId("count").textContent).toBe("0");
  });

  it("REGRESSION: uid switch resets state via the uid effect; a stale user-A refetch cannot publish and user-B baseline loads", async () => {
    // StrictMode/concurrent rendering must not surface render-phase update
    // errors after the reset moved out of render into the uid effect.
    const consoleErrors: string[] = [];
    vi.spyOn(console, "error").mockImplementation((message: unknown) => {
      consoleErrors.push(String(message));
    });
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve, reject) => requests.push({ resolve, reject }))));
    const view = render(
      <StrictMode>
        <SalesNotificationProvider>
          <Consumer />
        </SalesNotificationProvider>
      </StrictMode>
    );
    await screen.findByTestId("count");
    // StrictMode may double-invoke the baseline effect; resolve every pending
    // user-A baseline fetch with the same payload.
    await act(async () => {
      for (const pending of requests.splice(0)) pending.resolve(response({ items: [{ id: "old", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 1 }));
    });
    expect(await screen.findByText("old")).toBeTruthy();

    // An in-flight user-A refetch stays pending across the uid switch.
    await act(async () => { fcmHandlers[0]?.({ data: { type: "new_lead" } }); });
    await waitFor(() => expect(requests.length).toBeGreaterThanOrEqual(1));

    // Switch user A -> user B: user A state must disappear synchronously.
    authState.user = { uid: "uid-b", role: "sales" };
    view.rerender(
      <StrictMode>
        <SalesNotificationProvider>
          <Consumer />
        </SalesNotificationProvider>
      </StrictMode>
    );
    expect(screen.getByTestId("items").textContent).toBe("");
    expect(screen.getByTestId("count").textContent).toBe("0");

    // The stale user-A refetch resolves now: it must not publish (activeUidRef
    // guard) — user B's feed stays empty and uncounted.
    await act(async () => {
      requests.shift()?.resolve(response({ items: [{ id: "stale-a", lead_id: 9, project_key: "x", created_at: "2026-01-03", read_at: null }], unread_count: 7 }));
    });
    expect(screen.getByTestId("items").textContent).toBe("");
    expect(screen.getByTestId("count").textContent).toBe("0");

    // User B's baseline resolves: their own unread feed loads.
    await act(async () => {
      for (const pending of requests.splice(0)) pending.resolve(response({ items: [{ id: "b1", lead_id: 11, project_key: "x", created_at: "2026-01-04", read_at: null }], unread_count: 1 }));
    });
    expect(await screen.findByText("b1")).toBeTruthy();
    expect(screen.getByTestId("count").textContent).toBe("1");

    // No render-phase update loop: React must not have logged any
    // "cannot update" / render-phase setState warnings.
    expect(consoleErrors.filter((entry) => /update|setState|render/i.test(entry))).toEqual([]);
  });

  it("merges live arrivals that race with the initial fetch", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve, reject) => requests.push({ resolve, reject }))));
    renderProvider();
    await screen.findByTestId("count");
    streams[0]?.onLeadsChanged([lead("n1")]);
    streams[0]?.onLeadsChanged([lead("n1"), lead("live")]);
    await act(async () => requests.shift()?.resolve(response({ items: [{ id: "server", lead_id: 3, project_key: "x", created_at: "2026-01-02", read_at: null }], unread_count: 1 })));
    expect(screen.getByTestId("items").textContent).toContain("server");
    // Live items share the server's decimal id domain (str(leads.id)), not the HMAC doc id.
    expect(screen.getByTestId("items").textContent).toContain("2");
  });

  it("does not undercount when the server has more unread items than loaded", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ items: [], unread_count: 25 })));
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("25"));
  });

  it("shows toast and updated unread badge for a newly assigned lead", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ items: [], unread_count: 0 })));
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("0"));

    streams[0]?.onLeadsChanged([lead("live")]);

    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));
    expect(notificationOpen).toHaveBeenCalledWith(expect.objectContaining({
      message: "Lead mới",
      description: "live",
    }));
  });

  it("reconciles a refreshed server baseline without double counting", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 10 }))
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }, { id: "live", lead_id: 2, project_key: "x", created_at: "2026-01-02", read_at: null }], unread_count: 11 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("10"));
    streams[0]?.onLeadsChanged([lead("live")]);
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("11"));
    await act(async () => { await fetchMock.mock.results[0]?.value; });
    // A second provider mount starts a fresh fetch and establishes the same baseline.
    cleanup();
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("11"));
  });

  it("decrements the baseline when a server notification is read", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 10 }))
      .mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("10"));
    await act(async () => { screen.getByRole("button", { name: "read" }).click(); });
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("9"));
  });

  it("makes duplicate mark-read calls idempotent", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 1 }))
      .mockImplementationOnce(() => new Promise<Response>(() => undefined)));
    renderProvider();
    await screen.findByText("n1");
    await act(async () => { screen.getByRole("button", { name: "read" }).click(); screen.getByRole("button", { name: "read" }).click(); });
    expect(fetch).toHaveBeenCalledTimes(2);
    await act(async () => { vi.mocked(fetch).mockResolvedValue(response({})); });
  });

  it("PATCHes the decimal lead id with the read flag for a live lead", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [], unread_count: 0 }))
      .mockResolvedValue(response({ updated_ids: ["2"], unread_count: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("0"));
    streams[0]?.onLeadsChanged([lead("live")]);
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));
    await act(async () => { screen.getByRole("button", { name: "read" }).click(); });
    const [, init] = fetchMock.mock.calls[1];
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({ notification_ids: ["2"], read: true });
    expect(await waitFor(() => screen.getByTestId("error").textContent)).toBe("");
  });

  it("surfaces a non-2xx mark-read without unhandled rejection and keeps the badge", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 10 }))
      .mockResolvedValueOnce(failure(422));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("10"));
    await act(async () => { screen.getByRole("button", { name: "read" }).click(); });
    await waitFor(() => expect(screen.getByTestId("error").textContent).toBe("Không thể cập nhật thông báo."));
    // Reconciliation intact: no local read, no badge decrement on failure.
    expect(screen.getByTestId("count").textContent).toBe("10");
    expect(screen.getByTestId("items").textContent).toBe("n1");
  });

  it("clears the surfaced error when a retried mark-read succeeds", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 10 }))
      .mockResolvedValueOnce(failure(500))
      .mockResolvedValueOnce(response({ updated_ids: ["1"], unread_count: 9 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("10"));
    await act(async () => { screen.getByRole("button", { name: "read" }).click(); });
    await waitFor(() => expect(screen.getByTestId("error").textContent).not.toBe(""));
    await act(async () => { screen.getByRole("button", { name: "read" }).click(); });
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("9"));
    expect(screen.getByTestId("error").textContent).toBe("");
  });

  it("surfaces a failed mark-all-read without throwing", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 10 }))
      .mockResolvedValueOnce(failure(503));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("10"));
    await act(async () => { screen.getByRole("button", { name: "read-all" }).click(); });
    await waitFor(() => expect(screen.getByTestId("error").textContent).toBe("Không thể cập nhật thông báo."));
    expect(screen.getByTestId("count").textContent).toBe("10");
    expect(screen.getByTestId("items").textContent).toBe("n1");
  });

  it("refetches the unread list when a foreground FCM message arrives", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [], unread_count: 0 }))
      .mockResolvedValueOnce(response({ items: [{ id: "s1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 1 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("0"));
    await act(async () => { fcmHandlers[0]?.({ notification: { title: "Thông báo" }, data: { type: "new_lead" } }); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));
  });

  it("coalesces a burst of foreground messages into a single in-flight refetch", async () => {
    let resolveRefetch: (value: Response) => void = () => {};
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [], unread_count: 0 }))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveRefetch = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("0"));
    act(() => {
      fcmHandlers[0]?.({ notification: { title: "a" }, data: { type: "new_lead" } });
      fcmHandlers[0]?.({ notification: { title: "b" }, data: { type: "new_lead" } });
      fcmHandlers[0]?.({ notification: { title: "c" }, data: { type: "new_lead" } });
    });
    // Only one refetch is issued while the first is still in flight — no storm.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => { resolveRefetch(response({ items: [], unread_count: 0 })); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps existing items when a refetch fails", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [{ id: "n1", lead_id: 1, project_key: "x", created_at: "2026-01-01", read_at: null }], unread_count: 1 }))
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue(response({ items: [], unread_count: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));
    await act(async () => { fcmHandlers[0]?.({ notification: { title: "x" }, data: { type: "new_lead" } }); });
    await waitFor(() => expect(screen.getByTestId("error").textContent).toBe("Không thể làm mới thông báo."));
    // A failed refetch never resets items to empty.
    expect(screen.getByTestId("items").textContent).toBe("n1");
    expect(screen.getByTestId("count").textContent).toBe("1");
  });

  it("throttles focus-triggered refetches to once per 30 seconds", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ items: [], unread_count: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // Fake timers install at the captured wall clock, so advancing 31s clears
    // the 30s window that the baseline fetch stamped on mount.
    vi.useFakeTimers({ now: Date.now() });
    try {
      await act(async () => { vi.advanceTimersByTime(31_000); window.dispatchEvent(new Event("focus")); });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await act(async () => { window.dispatchEvent(new Event("focus")); });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await act(async () => { vi.advanceTimersByTime(31_000); window.dispatchEvent(new Event("focus")); });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a toast and refetches for a valid new_lead foreground payload", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ items: [], unread_count: 0 }))
      .mockResolvedValueOnce(response({ items: [{ id: "s9", lead_id: 9, project_key: "x", created_at: "2026-01-02", read_at: null }], unread_count: 1 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => { fcmHandlers[0]?.({ notification: { title: "Lead mới", body: "Chi nhánh Q7" }, data: { type: "new_lead", lead_id: "9" } }); });
    expect(notificationOpen).toHaveBeenCalledWith(expect.objectContaining({ message: "Lead mới", description: "Chi nhánh Q7" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));
  });

  it("does not leak phone/PII into the foreground toast", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ items: [], unread_count: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => { fcmHandlers[0]?.({ notification: { title: "Lead mới", body: "Khách cần tư vấn" }, data: { type: "new_lead", phone: "0909998887" } }); });
    const call = notificationOpen.mock.calls[0]?.[0] as { message: string; description: string } | undefined;
    expect(call).toBeTruthy();
    // The backend only ever sends masked phones; any raw phone in the payload
    // must not be echoed into UI surface strings.
    expect(call?.message).not.toContain("0909998887");
    expect(call?.description).not.toContain("0909998887");
  });

  it("ignores malformed and irrelevant foreground payloads without refetching", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ items: [], unread_count: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    renderProvider();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => {
      fcmHandlers[0]?.({ data: {} });
      fcmHandlers[0]?.({ data: { type: "client_only_event" } });
      fcmHandlers[0]?.({});
    });
    expect(notificationOpen).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
