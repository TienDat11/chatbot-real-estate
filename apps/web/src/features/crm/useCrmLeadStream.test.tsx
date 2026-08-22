// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import type {
  CrmLeadStreamRequest,
  LeadStreamHandlers,
} from "@/domain/crm/ports/leadRepositoryPort";
import type { RealtimeContainer } from "@/infrastructure/realtimeContainer";
import { RealtimeContainerContext } from "@/lib/realtime/RealtimeProvider";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";
import { useCrmLeadStream } from "@/features/crm/useCrmLeadStream";

interface FakeSubscription {
  request: CrmLeadStreamRequest;
  handlers: LeadStreamHandlers;
  unsubscribe: ReturnType<typeof vi.fn>;
}

function makeFakeContainer() {
  const subscriptions: FakeSubscription[] = [];
  const container = {
    leadRealtimeService: {
      streamLeadsForCrm: vi.fn(
        (request: CrmLeadStreamRequest, handlers: LeadStreamHandlers) => {
          const subscription: FakeSubscription = {
            request,
            handlers,
            unsubscribe: vi.fn(),
          };
          subscriptions.push(subscription);
          return { unsubscribe: subscription.unsubscribe };
        }
      ),
    },
  } as unknown as RealtimeContainer;
  return { container, subscriptions };
}

function wrapperWith(container: RealtimeContainer) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <RealtimeContainerContext.Provider value={container}>
        {children}
      </RealtimeContainerContext.Provider>
    );
  };
}

afterEach(() => {
  cleanup();
});

describe("useCrmLeadStream", () => {
  it("subscribes through the port with the sales isolation filter (rules where-clause)", () => {
    const { container, subscriptions } = makeFakeContainer();
    renderHook(
      () =>
        useCrmLeadStream({
          assignedSalesFirebaseUidFilter: "uid-sales-7",
        }),
      { wrapper: wrapperWith(container) }
    );
    expect(subscriptions).toHaveLength(1);
    expect(subscriptions[0].request).toEqual({
      assignedSalesFirebaseUid: "uid-sales-7",
    });
  });

  it("admin scope (null filter) subscribes without an isolation clause", () => {
    const { container, subscriptions } = makeFakeContainer();
    renderHook(
      () =>
        useCrmLeadStream({
          assignedSalesFirebaseUidFilter: null,
        }),
      { wrapper: wrapperWith(container) }
    );
    expect(subscriptions[0].request).toEqual({
      assignedSalesFirebaseUid: null,
    });
  });

  it("renders rows from a faked realtime subscription and notifies on live new leads only", async () => {
    const { container, subscriptions } = makeFakeContainer();
    const onIncomingLead = vi.fn();
    const { result } = renderHook(
      () =>
        useCrmLeadStream({
          assignedSalesFirebaseUidFilter: null,
          onIncomingLead,
        }),
      { wrapper: wrapperWith(container) }
    );

    const { handlers } = subscriptions[0];
    // First snapshot establishes the baseline: rows render, no notification.
    act(() => {
      handlers.onConnectionStateChanged?.("active");
      handlers.onLeadsChanged([
        makeCrmLeadFixture({ id: "lead-1" }),
        makeCrmLeadFixture({ id: "lead-2", createdAt: "2026-08-21T08:00:00.000Z" }),
      ]);
    });
    await waitFor(() => expect(result.current.leads).toHaveLength(2));
    expect(result.current.connectionState).toBe("active");
    expect(onIncomingLead).not.toHaveBeenCalled();

    // A live push with status "new" notifies; an assigned one does not. The
    // new arrival carries a newer mirror timestamp so it sorts first.
    act(() => {
      handlers.onLeadsChanged([
        makeCrmLeadFixture({
          id: "lead-3",
          workflowStatus: "new",
          createdAt: "2026-08-23T09:00:00.000Z",
        }),
        makeCrmLeadFixture({ id: "lead-4", workflowStatus: "assigned" }),
        makeCrmLeadFixture({ id: "lead-1" }),
        makeCrmLeadFixture({ id: "lead-2", createdAt: "2026-08-21T08:00:00.000Z" }),
      ]);
    });
    await waitFor(() => expect(result.current.leads).toHaveLength(4));
    expect(onIncomingLead).toHaveBeenCalledTimes(1);
    expect(onIncomingLead).toHaveBeenCalledWith(
      expect.objectContaining({ id: "lead-3", workflowStatus: "new" })
    );
    // Newest first ordering.
    expect(result.current.leads[0].id).toBe("lead-3");
  });

  it("applies optimistic patches and releases them once the snapshot catches up", async () => {
    const { container, subscriptions } = makeFakeContainer();
    const { result } = renderHook(
      () =>
        useCrmLeadStream({
          assignedSalesFirebaseUidFilter: null,
        }),
      { wrapper: wrapperWith(container) }
    );

    act(() => {
      subscriptions[0].handlers.onLeadsChanged([
        makeCrmLeadFixture({ id: "lead-1", workflowStatus: "new" }),
      ]);
    });
    await waitFor(() => expect(result.current.leads).toHaveLength(1));

    act(() => {
      result.current.applyOptimisticLeadPatch("lead-1", { workflowStatus: "called" });
    });
    await waitFor(() =>
      expect(result.current.leads[0].workflowStatus).toBe("called")
    );

    // The mirror catching up (snapshot now says "called") must release the
    // patch so long-term state is purely authoritative.
    act(() => {
      subscriptions[0].handlers.onLeadsChanged([
        makeCrmLeadFixture({ id: "lead-1", workflowStatus: "called" }),
      ]);
    });
    await waitFor(() =>
      expect(result.current.leads[0].workflowStatus).toBe("called")
    );
    expect(result.current.leads[0]).toEqual(
      makeCrmLeadFixture({ id: "lead-1", workflowStatus: "called" })
    );
  });

  it("clears a failed optimistic patch on demand and unsubscribes on unmount", async () => {
    const { container, subscriptions } = makeFakeContainer();
    const { result, unmount } = renderHook(
      () =>
        useCrmLeadStream({
          assignedSalesFirebaseUidFilter: null,
        }),
      { wrapper: wrapperWith(container) }
    );

    act(() => {
      subscriptions[0].handlers.onLeadsChanged([
        makeCrmLeadFixture({ id: "lead-1", workflowStatus: "new" }),
      ]);
    });
    await waitFor(() => expect(result.current.leads).toHaveLength(1));

    act(() => {
      result.current.applyOptimisticLeadPatch("lead-1", { workflowStatus: "booked" });
    });
    act(() => {
      result.current.clearOptimisticLeadPatch("lead-1");
    });
    await waitFor(() =>
      expect(result.current.leads[0].workflowStatus).toBe("new")
    );

    unmount();
    expect(subscriptions[0].unsubscribe).toHaveBeenCalledTimes(1);
  });
});
