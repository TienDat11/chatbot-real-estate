// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Lead } from "@/domain/crm/lead";
import type { CrmLeadsPageFilters, CrmLeadsPageResult } from "@/domain/crm/leadPage";
import { makeCrmLeadFixture } from "./crmLeadFixture";
import { CRM_LEADS_DEFAULT_PAGE_SIZE, REALTIME_REFRESH_COALESCE_MS, useCrmLeadsPage } from "./useCrmLeadsPage";

/**
 * Realtime-vs-paging reconciliation contract (bug wave: newest Firestore lead
 * must appear immediately at the top of page 1 while the table baseline stays
 * server-paginated REST data). fetchCrmLeadsPage is mocked at the module
 * boundary; the realtime stream feeds the hook through reconcileRealtimeSnapshot.
 */
const fetchPageMock = vi.fn<(request: {
  cursor: string | null;
  limit: number;
  projectKey: string | null;
  status: string | null;
}) => Promise<CrmLeadsPageResult>>();

vi.mock("./crmApiClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./crmApiClient")>()),
  fetchCrmLeadsPage: (request: Parameters<typeof fetchPageMock>[0]) =>
    fetchPageMock(request),
}));

const NO_FILTERS: CrmLeadsPageFilters = {
  projectKey: null,
  status: null,
  reengageFromIsoDate: null,
  reengageToIsoDate: null,
};

function makePage(
  rows: Lead[],
  hasMore = false,
  nextCursor: string | null = null
): CrmLeadsPageResult {
  return { rows, nextCursor, hasMore, serverTime: "2026-09-03T00:00:00Z" };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const getBearerToken = vi.fn<() => Promise<string | null>>(async () => "token-1");

/** REST page with one historical row (server order: newest first). */
const HISTORICAL = makeCrmLeadFixture({
  id: "rest-1",
  createdAt: "2026-09-01T08:00:00.000Z",
  updatedAt: "2026-09-01T08:00:00.000Z",
});

function renderCrmPage(filters: CrmLeadsPageFilters = NO_FILTERS) {
  return renderHook(() => useCrmLeadsPage({ filters, getBearerToken }));
}

beforeEach(() => {
  // reset (not clear) so a failed test's mockResolvedValueOnce queue can never
  // leak into the next test.
  vi.resetAllMocks();
  getBearerToken.mockResolvedValue("token-1");
  fetchPageMock.mockResolvedValue(makePage([HISTORICAL], false, null));
});

afterEach(() => {
  cleanup();
});

describe("useCrmLeadsPage realtime reconciliation", () => {
  it("REGRESSION: a newly observed realtime lead appears at the top of page 1 while the REST baseline rows stay", async () => {
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]));

    // First snapshot = historical baseline; must not mass-prepend.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);

    // A NEW lead arrives live — it must surface immediately on top.
    const fresh = makeCrmLeadFixture({
      id: "live-new",
      createdAt: "2026-09-03T10:00:00.000Z",
      updatedAt: "2026-09-03T10:00:00.000Z",
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL, fresh]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["live-new", "rest-1"]);
  });

  it("dedupes by stable id: the same lead arriving twice renders once", async () => {
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });

    const fresh = makeCrmLeadFixture({
      id: "live-new",
      createdAt: "2026-09-03T10:00:00.000Z",
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([fresh]);
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([fresh, fresh]);
    });
    expect(result.current.rows.filter((r) => r.id === "live-new")).toHaveLength(1);
    expect(result.current.rows).toHaveLength(2);
  });

  it("orders inserts newest-first by createdAt, tie-break by stable id", async () => {
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });

    const a = makeCrmLeadFixture({ id: "aaa", createdAt: "2026-09-03T09:00:00.000Z" });
    const b = makeCrmLeadFixture({ id: "bbb", createdAt: "2026-09-03T09:00:00.000Z" });
    const c = makeCrmLeadFixture({ id: "ccc", createdAt: "2026-09-03T11:00:00.000Z" });
    act(() => {
      result.current.reconcileRealtimeSnapshot([a]);
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([a, b]);
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([a, b, c]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["ccc", "bbb", "aaa", "rest-1"]);
  });

  it("does not insert a realtime lead that fails the project or status filter", async () => {
    // Reconcile under NO_FILTERS first so the historical baseline is recorded;
    // the status filter change then re-baselines via the stream's own snapshot.
    const { result, rerender } = renderHook(
      ({ filters }: { filters: CrmLeadsPageFilters }) =>
        useCrmLeadsPage({ filters, getBearerToken }),
      { initialProps: { filters: NO_FILTERS } }
    );
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });

    const soleilLead = makeCrmLeadFixture({
      id: "soleil-1",
      projectKey: "soleil",
      createdAt: "2026-09-03T10:00:00.000Z",
    });
    rerender({ filters: { ...NO_FILTERS, projectKey: "camellia" } });
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(2));
    // The filter change re-baselines the overlay: this snapshot is historical.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([soleilLead]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);

    rerender({ filters: { ...NO_FILTERS, projectKey: "camellia", status: "lost" } });
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(3));
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    const lostLead = makeCrmLeadFixture({
      id: "new-lost",
      workflowStatus: "new",
      createdAt: "2026-09-03T11:00:00.000Z",
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([lostLead]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);
  });

  it("uses a coalesced refresh (not a guessed insert) when the reengage window filter is active", async () => {
    const { result } = renderCrmPage({
      ...NO_FILTERS,
      reengageFromIsoDate: "2026-09-01",
      reengageToIsoDate: "2026-09-30",
    });
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]));
    expect(fetchPageMock).toHaveBeenCalledTimes(1);

    const reengaged = makeCrmLeadFixture({
      id: "live-reengage",
      reengageAt: "2026-09-05T02:00:00.000Z",
      createdAt: "2026-09-03T10:00:00.000Z",
    });
    // First snapshot is the historical baseline; then a burst of two identical
    // snapshots reports the new row (with the date window active the FE cannot
    // prove a calendar-day match, so nothing may be inserted directly).
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL, reengaged]);
      result.current.reconcileRealtimeSnapshot([HISTORICAL, reengaged]);
    });
    // Not guessed: no insert before the coalesced refresh fires.
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);

    await waitFor(
      () => expect(fetchPageMock).toHaveBeenCalledTimes(2),
      { timeout: 3_000 }
    );
    // The burst collapsed into ONE page-0 refetch (cursor null), and the
    // authoritative server response re-baselines the rows.
    expect(fetchPageMock.mock.calls).toHaveLength(2);
    expect(fetchPageMock.mock.calls[1][0].cursor).toBeNull();
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);
  }, 10_000);

  it("never inserts unseen newest realtime rows into a later cursor page", async () => {
    fetchPageMock.mockResolvedValueOnce(makePage([HISTORICAL], true, "cursor-1"));
    fetchPageMock.mockResolvedValueOnce(
      makePage([makeCrmLeadFixture({ id: "page2-row", createdAt: "2026-08-30T08:00:00.000Z" })], false, null)
    );
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]));
    // Establish the stream baseline before navigating away from page 0.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });

    act(() => result.current.goToPage(1));
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["page2-row"]));
    expect(result.current.pageIndex).toBe(1);

    const fresh = makeCrmLeadFixture({
      id: "live-new",
      createdAt: "2026-09-03T10:00:00.000Z",
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([fresh]);
    });
    // Cursor integrity: page 2 keeps its server rows; nothing is prepended.
    expect(result.current.rows.map((r) => r.id)).toEqual(["page2-row"]);

    // Returning to page 0 refetches from the server and shows the new lead.
    fetchPageMock.mockResolvedValueOnce(
      makePage([fresh, HISTORICAL], false, null)
    );
    act(() => result.current.goToPage(0));
    await waitFor(() =>
      expect(result.current.rows.map((r) => r.id)).toEqual(["live-new", "rest-1"])
    );
    expect(fetchPageMock.mock.calls[2][0].cursor).toBeNull();
  });

  it("updates an already-visible row on any page from a realtime snapshot", async () => {
    fetchPageMock.mockResolvedValueOnce(makePage([HISTORICAL], true, "cursor-1"));
    // Page 2 also contains the historical row: the realtime update must reach
    // a twin that is visible on a NON-zero page.
    fetchPageMock.mockResolvedValueOnce(makePage([HISTORICAL], false, null));
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    // Establish the stream baseline before navigating away from page 0.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    act(() => result.current.goToPage(1));
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(2));

    const updated = {
      ...HISTORICAL,
      workflowStatus: "booked" as const,
      updatedAt: "2026-09-03T09:00:00.000Z",
    };
    act(() => {
      result.current.reconcileRealtimeSnapshot([updated]);
    });
    expect(result.current.rows).toHaveLength(1);
    expect(result.current.rows[0].workflowStatus).toBe("booked");
    expect(result.current.pageIndex).toBe(1);
  });

  it("does not let a resolving REST response overwrite a newer realtime insert (stale race)", async () => {
    const stale = deferred<CrmLeadsPageResult>();
    fetchPageMock.mockImplementationOnce(() => stale.promise);
    const { result } = renderCrmPage();
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(1));

    const fresh = makeCrmLeadFixture({
      id: "live-new",
      createdAt: "2026-09-03T10:00:00.000Z",
      updatedAt: "2026-09-03T10:00:00.000Z",
    });
    // The first snapshot (before the initial REST page resolves) is itself the
    // historical baseline... but it contains the new lead, so the next
    // snapshot (after the baseline snapshot exists) is what proves the insert.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL, fresh]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["live-new"]);

    // The in-flight REST response (started before the event) resolves now.
    await act(async () => {
      stale.resolve(makePage([HISTORICAL], false, null));
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["live-new", "rest-1"]);
  });

  it("clears the pending coalesced realtime refresh on unmount (cleanup)", async () => {
    const { result, unmount } = renderCrmPage({
      ...NO_FILTERS,
      reengageFromIsoDate: "2026-09-01",
      reengageToIsoDate: "2026-09-30",
    });
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(1));
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    const fresh = makeCrmLeadFixture({
      id: "live-reengage",
      createdAt: "2026-09-03T10:00:00.000Z",
    });
    // A new row under an active date window schedules the coalesced refresh.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL, fresh]);
    });
    // Unmount while the REALTIME_REFRESH_COALESCE_MS timer is still pending:
    // no state update after unmount and no refetch may ever fire.
    unmount();
    await new Promise((resolve) =>
      setTimeout(resolve, REALTIME_REFRESH_COALESCE_MS + 150)
    );
    expect(fetchPageMock).toHaveBeenCalledTimes(1);
  }, 10_000);

  it("resets the realtime baseline when the filter set changes", async () => {
    const { result, rerender } = renderHook(
      ({ filters }: { filters: CrmLeadsPageFilters }) =>
        useCrmLeadsPage({ filters, getBearerToken }),
      { initialProps: { filters: NO_FILTERS } }
    );
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });

    rerender({ filters: { ...NO_FILTERS, projectKey: "camellia" } });
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(2));

    // The same lead seen before the filter change must not double-insert.
    act(() => {
      result.current.reconcileRealtimeSnapshot([HISTORICAL]);
    });
    expect(result.current.rows.filter((r) => r.id === "rest-1")).toHaveLength(1);
  });

  it("REGRESSION: a baseline-only historical lead never renders on page 0; its REST twin still updates in place on a later page", async () => {
    // REST page 0 does NOT contain the historical lead H; H lives on a later
    // server page. The stream's first snapshot (the historical baseline) is H
    // itself, and a later snapshot repeats H: baseline membership alone must
    // never make H renderable on page 0 (no insertedIds entry -> no overlay
    // insert), while the REST twin on the later page must still receive
    // realtime updates in place.
    fetchPageMock.mockResolvedValueOnce(makePage([HISTORICAL], true, "cursor-1"));
    fetchPageMock.mockResolvedValueOnce(
      makePage([makeCrmLeadFixture({ id: "historical-h", createdAt: "2026-08-25T08:00:00.000Z" })], false, null)
    );
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]));

    const historicalH = makeCrmLeadFixture({
      id: "historical-h",
      createdAt: "2026-08-25T08:00:00.000Z",
    });
    // Snapshot 1 is the historical baseline (H is recorded, never rendered).
    act(() => {
      result.current.reconcileRealtimeSnapshot([historicalH]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);

    // Snapshot 2 contains H again: known-before id -> update overlay only.
    // H is not in REST page 0 and not in realtimeInsertedIdsRef, so it must
    // never appear as an overlay-only row here.
    act(() => {
      result.current.reconcileRealtimeSnapshot([historicalH]);
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["rest-1"]);
    expect(result.current.rows).toHaveLength(1);

    // Navigate to the later page holding H's REST twin; a realtime change to H
    // must still update that twin in place (update overlays are page-agnostic).
    act(() => result.current.goToPage(1));
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["historical-h"]));

    const updatedH = {
      ...historicalH,
      workflowStatus: "booked" as const,
      updatedAt: "2026-09-03T09:00:00.000Z",
    };
    act(() => {
      result.current.reconcileRealtimeSnapshot([updatedH]);
    });
    expect(result.current.rows).toHaveLength(1);
    expect(result.current.rows[0].id).toBe("historical-h");
    expect(result.current.rows[0].workflowStatus).toBe("booked");
    expect(result.current.pageIndex).toBe(1);
  });

  it("keeps the server page size untouched by realtime inserts", async () => {
    const baseline = Array.from({ length: CRM_LEADS_DEFAULT_PAGE_SIZE }, (_, i) =>
      makeCrmLeadFixture({ id: `rest-${i}`, createdAt: `2026-09-0${(i % 9) + 1}T08:00:00.000Z` })
    );
    fetchPageMock.mockResolvedValueOnce(makePage(baseline, false, null));
    const { result } = renderCrmPage();
    await waitFor(() => expect(result.current.rows).toHaveLength(CRM_LEADS_DEFAULT_PAGE_SIZE));
    act(() => {
      result.current.reconcileRealtimeSnapshot(baseline);
    });

    const fresh = makeCrmLeadFixture({
      id: "live-new",
      createdAt: "2026-09-03T10:00:00.000Z",
    });
    act(() => {
      result.current.reconcileRealtimeSnapshot([fresh]);
    });
    // Realtime inserts overlay the current page; the hook still reports the
    // server page size, and the next server refetch re-baselines the rows.
    expect(result.current.pageSize).toBe(CRM_LEADS_DEFAULT_PAGE_SIZE);
    expect(result.current.rows[0].id).toBe("live-new");
    expect(result.current.rows).toHaveLength(CRM_LEADS_DEFAULT_PAGE_SIZE + 1);
  });
});
