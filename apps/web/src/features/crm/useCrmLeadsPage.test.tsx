// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Lead } from "@/domain/crm/lead";
import { makeCrmLeadFixture } from "./crmLeadFixture";
import type { CrmLeadsPageResult } from "@/domain/crm/leadPage";
import type { CrmLeadsPageFilters } from "@/domain/crm/leadPage";
import { CRM_LEADS_DEFAULT_PAGE_SIZE, useCrmLeadsPage } from "./useCrmLeadsPage";

/**
 * fetchCrmLeadsPage is mocked at the module boundary (the network layer stays
 * covered by crmApiClient.test.ts); here we verify the hook's paging contract:
 * keyset cursors, filter resets, stale-response suppression and auth errors.
 */
const fetchPageMock = vi.fn<(request: {
  cursor: string | null;
  limit: number;
  signal: AbortSignal;
  projectKey: string | null;
  status: string | null;
}) => Promise<CrmLeadsPageResult>>();

vi.mock("./crmApiClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./crmApiClient")>()),
  fetchCrmLeadsPage: (request: Parameters<typeof fetchPageMock>[0]) =>
    fetchPageMock(request),
}));

import { CrmApiClientError } from "./crmApiClient";

const NO_FILTERS: CrmLeadsPageFilters = {
  projectKey: null,
  status: null,
  reengageFromIsoDate: null,
  reengageToIsoDate: null,
};

function makePage(rows: Lead[], hasMore = false, nextCursor: string | null = null): CrmLeadsPageResult {
  return { rows, nextCursor, hasMore, serverTime: "2026-09-03T00:00:00Z" };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (cause: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Stable bearer seam: a non-null token unless the test overrides it. */
const getBearerToken = vi.fn<() => Promise<string | null>>(async () => "token-1");

beforeEach(() => {
  vi.clearAllMocks();
  getBearerToken.mockResolvedValue("token-1");
  fetchPageMock.mockResolvedValue(makePage([], false, null));
});

afterEach(() => {
  cleanup();
});

describe("useCrmLeadsPage", () => {
  it("fetches page 0 with the given filters, limit 20 and a null cursor", async () => {
    const { result } = renderHook(() =>
      useCrmLeadsPage({ filters: { ...NO_FILTERS, projectKey: "camellia" }, getBearerToken })
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchPageMock).toHaveBeenCalledTimes(1);
    const request = fetchPageMock.mock.calls[0][0];
    expect(request.cursor).toBeNull();
    expect(request.limit).toBe(CRM_LEADS_DEFAULT_PAGE_SIZE);
    expect(request.projectKey).toBe("camellia");
    expect(request.status).toBeNull();
    expect(result.current.pageIndex).toBe(0);
    expect(result.current.hasNextPage).toBe(false);
    expect(result.current.hasPreviousPage).toBe(false);
  });

  it("sends the reengage window and status verbatim to the server (no local date shifting)", async () => {
    const filters: CrmLeadsPageFilters = {
      projectKey: null,
      status: "lost",
      reengageFromIsoDate: "2026-09-01",
      reengageToIsoDate: "2026-09-30",
    };
    const { result } = renderHook(() => useCrmLeadsPage({ filters, getBearerToken }));

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const request = fetchPageMock.mock.calls[0][0];
    expect(request.status).toBe("lost");
    expect(fetchPageMock.mock.calls[0][0]).toMatchObject({
      reengageFromIsoDate: "2026-09-01",
      reengageToIsoDate: "2026-09-30",
    });
  });

  it("resets the cursor stack and reloads page 0 when filters change", async () => {
    fetchPageMock.mockResolvedValueOnce(makePage([makeCrmLeadFixture()], true, "cursor-1"));
    const { result, rerender } = renderHook(
      ({ filters }: { filters: CrmLeadsPageFilters }) =>
        useCrmLeadsPage({ filters, getBearerToken }),
      { initialProps: { filters: NO_FILTERS } }
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.hasNextPage).toBe(true);

    rerender({ filters: { ...NO_FILTERS, projectKey: "soleil" } });
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(2));
    // Debounce (300 ms) settles the new filter set before the request fires.
    await waitFor(
      () => expect(fetchPageMock.mock.calls[1][0]).toMatchObject({ projectKey: "soleil", cursor: null, limit: CRM_LEADS_DEFAULT_PAGE_SIZE }),
      { timeout: 2000 }
    );
    expect(result.current.pageIndex).toBe(0);
  });

  it("navigates next with the server's cursor and back to a visited page by its start cursor", async () => {
    const page1 = makePage([makeCrmLeadFixture({ id: "a" })], true, "cursor-1");
    const page2 = makePage([makeCrmLeadFixture({ id: "b" })], false, null);
    fetchPageMock.mockImplementation(async ({ cursor }) =>
      cursor === null ? page1 : page2
    );
    const { result } = renderHook(() => useCrmLeadsPage({ filters: NO_FILTERS, getBearerToken }));
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect(result.current.hasNextPage).toBe(true);

    act(() => result.current.goToPage(1));
    await waitFor(() => expect(result.current.pageIndex).toBe(1));
    expect(fetchPageMock.mock.calls[1][0].cursor).toBe("cursor-1");
    await waitFor(() => expect(result.current.hasNextPage).toBe(false));
    expect(result.current.hasPreviousPage).toBe(true);

    act(() => result.current.goToPage(0));
    await waitFor(() => expect(result.current.pageIndex).toBe(0));
    await waitFor(() => expect(fetchPageMock.mock.calls[2][0].cursor).toBeNull());
    expect(result.current.hasNextPage).toBe(true);
  });

  it("accepts forward navigation even when the target cursor is not yet primed", async () => {
    // Regression: the old guard rejected any target whose cursor was not in the
    // stack, so a "Next" click did nothing right after a filter/pageSize reset
    // (stack = [null]) or before the next cursor resolved. Forward must always
    // advance; only backward navigation needs a known cursor.
    fetchPageMock.mockImplementation(async ({ cursor }) =>
      cursor === null
        ? makePage([makeCrmLeadFixture({ id: "a" })], true, "cursor-1")
        : makePage([makeCrmLeadFixture({ id: "b" })], true, "cursor-2")
    );
    const { result } = renderHook(() =>
      useCrmLeadsPage({ filters: NO_FILTERS, getBearerToken })
    );
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    // Only page 1's cursor is primed after page 0 resolves; page 2 is not.
    expect(result.current.pageIndex).toBe(0);

    act(() => result.current.goToPage(2));
    await waitFor(() => expect(result.current.pageIndex).toBe(2));
  });

  it("ignores a stale response that resolves after a newer request", async () => {
    const stale = deferred<CrmLeadsPageResult>();
    fetchPageMock.mockImplementationOnce(() => stale.promise);
    fetchPageMock.mockResolvedValueOnce(makePage([makeCrmLeadFixture({ id: "fresh" })]));

    const { result, rerender } = renderHook(
      ({ filters }: { filters: CrmLeadsPageFilters }) =>
        useCrmLeadsPage({ filters, getBearerToken }),
      { initialProps: { filters: NO_FILTERS } }
    );
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(1));

    rerender({ filters: { ...NO_FILTERS, status: "new" } });
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(["fresh"]));

    // The old (aborted) request finally resolves — it must not win.
    await act(async () => {
      stale.resolve(makePage([makeCrmLeadFixture({ id: "stale" })], true, "stale-cursor"));
    });
    expect(result.current.rows.map((r) => r.id)).toEqual(["fresh"]);
    expect(result.current.hasNextPage).toBe(false);
  });

  it("surfaces 401 as an auth error, clears rows and offers retry", async () => {
    fetchPageMock.mockRejectedValueOnce(new CrmApiClientError(401, "expired"));
    const { result } = renderHook(() => useCrmLeadsPage({ filters: NO_FILTERS, getBearerToken }));

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.isAuthError).toBe(true);
    expect(result.current.rows).toEqual([]);

    fetchPageMock.mockResolvedValueOnce(makePage([makeCrmLeadFixture()]));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.error).toBeNull());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
  });

  it("treats a null bearer token as a re-login auth error", async () => {
    getBearerToken.mockResolvedValue(null);
    const { result } = renderHook(() => useCrmLeadsPage({ filters: NO_FILTERS, getBearerToken }));

    await waitFor(() => expect(result.current.error?.isAuthError).toBe(true));
    expect(fetchPageMock).not.toHaveBeenCalled();
  });

  it("changes page size, resets to page 0 and refetches with the new limit", async () => {
    // Page 0 must expose a next cursor so page 1 becomes a visited target.
    fetchPageMock.mockResolvedValueOnce(makePage([makeCrmLeadFixture()], true, "cursor-1"));
    fetchPageMock.mockResolvedValueOnce(makePage([], false, null));
    const { result } = renderHook(() =>
      useCrmLeadsPage({ filters: NO_FILTERS, getBearerToken, initialPageSize: 20 })
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    act(() => result.current.goToPage(1));
    await waitFor(() => expect(result.current.pageIndex).toBe(1));
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(2));

    act(() => result.current.changePageSize(50));
    await waitFor(() => expect(fetchPageMock).toHaveBeenCalledTimes(3));
    expect(result.current.pageIndex).toBe(0);
    expect(fetchPageMock.mock.calls[2][0].cursor).toBeNull();
    expect(fetchPageMock.mock.calls[2][0].limit).toBe(50);
  });

  it("exposes a non-auth failure with a retry that recovers", async () => {
    fetchPageMock.mockRejectedValueOnce(new Error("network down"));
    const { result } = renderHook(() => useCrmLeadsPage({ filters: NO_FILTERS, getBearerToken }));

    await waitFor(() => expect(result.current.error?.message).toBe("network down"));
    expect(result.current.error?.isAuthError).toBe(false);

    fetchPageMock.mockResolvedValueOnce(makePage([makeCrmLeadFixture()]));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
  });
});
