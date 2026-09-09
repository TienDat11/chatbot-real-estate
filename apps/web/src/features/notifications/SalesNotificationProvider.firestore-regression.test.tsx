// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { SalesNotificationProvider, useSalesNotifications } from "./SalesNotificationProvider";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";

const authState = { user: { uid: "sales-current", role: "sales" as const }, loading: false };
const subscriptions: Array<{ emit: (leads: ReturnType<typeof makeCrmLeadFixture>[]) => void; reconnect: () => void }> = [];
const notificationOpen = vi.hoisted(() => vi.fn());
const browserNotification = vi.hoisted(() => vi.fn());

vi.mock("@/lib/AuthProvider", () => ({ useAuth: () => authState }));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  getFreshIdToken: vi.fn(async () => "token"),
}));
vi.mock("@/features/crm/useCrmLeadStream", () => ({
  CrmLeadStreamProvider: ({ options, children }: { options: { onIncomingLead?: (lead: ReturnType<typeof makeCrmLeadFixture>) => void }; children: React.ReactNode }) => {
    let baseline = true;
    let seenIds = new Set<string>();
    const subscription = {
      reconnect: () => {
        baseline = true;
        seenIds = new Set<string>();
      },
      emit: (leads: ReturnType<typeof makeCrmLeadFixture>[]) => {
        const incoming = leads.filter((lead) => !seenIds.has(lead.id));
        if (!baseline) {
          for (const lead of incoming) {
            if (lead.assignedSalesFirebaseUid === authState.user.uid) {
              options.onIncomingLead?.(lead);
            }
          }
        }
        baseline = false;
        seenIds = new Set(leads.map((lead) => lead.id));
      },
    };
    subscriptions.push(subscription);
    return children;
  },
}));
vi.mock("@/infrastructure/firebase/firebaseMessaging", () => ({ registerFcmToken: vi.fn(async () => null), subscribeToFcmMessages: vi.fn(async () => () => undefined) }));
vi.mock("@/features/crm/browserLeadNotifier", () => ({ showBrowserLeadNotification: browserNotification }));
vi.mock("antd", () => ({ App: { useApp: () => ({ notification: { open: notificationOpen } }) } }));

function Consumer() {
  const state = useSalesNotifications();
  return <output data-testid="count">{state?.unreadCount}</output>;
}

function lead(id: string, assignedSalesFirebaseUid: string | null) {
  return makeCrmLeadFixture({
    id,
    leadId: id === "baseline" ? 100 : 101,
    name: id,
    workflowStatus: "assigned",
    assignedSalesFirebaseUid,
    createdAt: `2026-08-2${id === "baseline" ? "0" : "1"}T08:00:00.000Z`,
  });
}

function renderProvider() {
  return render(
    <SalesNotificationProvider>
      <Consumer />
    </SalesNotificationProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  notificationOpen.mockReset();
  browserNotification.mockReset();
  subscriptions.length = 0;
  authState.user = { uid: "sales-current", role: "sales" };
});

describe("SalesNotificationProvider Firestore lead regression", () => {
  it("suppresses the initial assigned snapshot, then announces one current-sales lead with badge and aria-live", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ items: [], unread_count: 0 }),
    })));
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("0"));

    act(() => subscriptions[0].emit([lead("baseline", "sales-current")]));
    expect(notificationOpen).not.toHaveBeenCalled();
    expect(screen.getByTestId("count").textContent).toBe("0");
    expect(screen.queryByText("0 thông báo chưa đọc")).toBeNull();

    act(() => subscriptions[0].emit([lead("baseline", "sales-current"), lead("new-lead", "sales-current")]));

    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));
    expect(notificationOpen).toHaveBeenCalledTimes(1);
    expect(notificationOpen).toHaveBeenCalledWith(expect.objectContaining({ message: "Lead mới", description: "new-lead" }));
    const liveRegion = screen.getByText("1 thông báo chưa đọc");
    expect(liveRegion.getAttribute("aria-live")).toBe("polite");
  });

  it("ignores another sales assignment and suppresses the same lead after reconnect", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ items: [], unread_count: 0 }),
    })));
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("0"));

    act(() => subscriptions[0].emit([lead("baseline", "sales-current")]));
    act(() => subscriptions[0].emit([lead("baseline", "sales-current"), lead("other-sales", "sales-other")]));
    expect(notificationOpen).not.toHaveBeenCalled();
    expect(screen.getByTestId("count").textContent).toBe("0");

    subscriptions[0].reconnect();
    act(() => subscriptions[0].emit([lead("baseline", "sales-current"), lead("other-sales", "sales-other")]));
    expect(notificationOpen).not.toHaveBeenCalled();
    expect(notificationOpen).not.toHaveBeenCalled();
  });
});
