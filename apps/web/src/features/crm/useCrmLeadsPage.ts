"use client";

/**
 * useCrmLeadsPage — server-paged CRM leads (plan FE-CRM-PAGING, FR-31).
 *
 * Replaces the realtime get-all snapshot as the TABLE's row source: rows come
 * from GET /api/crm/leases via crmApiClient.fetchCrmLeadsPage — keyset-cursor
 * paging, no client get-all, no local pagination. Owns:
 *  - a cursor STACK (start cursor per visited page) so prev/next navigation is
 *    deterministic: page i is always fetched with the cursor that started it;
 *  - request sequencing + AbortController so a stale response can never
 *    overwrite a newer page;
 *  - a debounce on filter changes (RangePicker fires per bound) that resets
 *    the cursor stack back to page 0;
 *  - explicit error states; 401/403 surface as isAuthError for a re-login
 *    prompt — there is NO silent fallback to any all-data source.
 *
 * The realtime stream stays with CrmWorkspace only for notifications,
 * deep-link selection and same-customer grouping. Its Firestore snapshots are
 * ALSO fed back here through reconcileRealtimeSnapshot as a thin overlay on
 * the current REST page:
 *  - the FIRST snapshot after mount or a filter reset is the historical
 *    baseline — its ids are recorded but never inserted (no mass-prepend of
 *    old Firestore rows over the server-paginated baseline);
 *  - afterwards, newly observed matching rows are inserted on page 0 only
 *    (newest-first by createdAt, tie-break stable id); later cursor pages are
 *    never reordered — an unseen newest row must not corrupt their keyset
 *    semantics;
 *  - already-visible rows are updated in place on any page (a realtime change
 *    always wins over an older REST twin, so a resolving REST response cannot
 *    overwrite newer realtime state);
 *  - when the reengage date window is active the FE cannot prove a filter
 *    match (the backend resolves calendar days in Asia/Ho_Chi_Minh), so a new
 *    row triggers one coalesced page-0 refresh instead of a guessed insert —
 *    refetch storms are avoided by design.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Lead } from "@/domain/crm/lead";
import type { CrmLeadsPageFilters } from "@/domain/crm/leadPage";
import { CrmApiClientError, fetchCrmLeadsPage } from "./crmApiClient";

/** Page sizes the backend accepts (limit 1..100; UI offers the sane subset). */
export const CRM_LEADS_PAGE_SIZES = [10, 20, 50, 100];
export const CRM_LEADS_DEFAULT_PAGE_SIZE = 20;

/** Filter edits settle this long before a user finishes a second action. */
const FILTER_DEBOUNCE_MS = 300;

/**
 * Coalescing window for realtime-triggered page-0 refreshes: bursts of new
 * Firestore rows collapse into ONE refetch (no refetch storms).
 */
export const REALTIME_REFRESH_COALESCE_MS = 800;

export interface CrmLeadsPageError {
  message: string;
  status: number | null;
  /** 401/403 (or a missing bearer): the user must re-authenticate. */
  isAuthError: boolean;
}

export interface UseCrmLeadsPageOptions {
  filters: CrmLeadsPageFilters;
  /** Mints a fresh Firebase ID token; null means signed out. */
  getBearerToken: () => Promise<string | null>;
  initialPageSize?: number;
}

export interface UseCrmLeadsPageResult {
  rows: Lead[];
  /** Zero-based page index (AntD pagination is one-based; convert at the edge). */
  pageIndex: number;
  pageSize: number;
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  isLoading: boolean;
  error: CrmLeadsPageError | null;
  /** Navigates to a visited (or the next) page; out-of-range targets are ignored. */
  goToPage: (pageIndex: number) => void;
  /** Changes the page size and resets to page 0 (new limit = new cursors). */
  changePageSize: (pageSize: number) => void;
  /** Re-fetches the current page (network failure recovery). */
  retry: () => void;
  /**
   * Feeds one Firestore snapshot into the realtime overlay (see the module
   * docblock for the baseline / insert / update / refresh contract).
   */
  reconcileRealtimeSnapshot: (leads: readonly Lead[]) => void;
}

function toPageError(cause: unknown): CrmLeadsPageError {
  if (cause instanceof CrmApiClientError) {
    return {
      message: cause.message,
      status: cause.status,
      isAuthError: cause.status === 401 || cause.status === 403,
    };
  }
  const fallback =
    cause instanceof Error ? cause.message : "Không tải được danh sách lead.";
  return { message: fallback, status: null, isAuthError: false };
}

const AUTH_ERROR: CrmLeadsPageError = {
  message: "Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.",
  status: null,
  isAuthError: true,
};

/** Newest-first overlay ordering: createdAt desc, then stable id desc. */
function compareNewestFirst(a: Lead, b: Lead): number {
  if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? 1 : -1;
  if (a.id !== b.id) return a.id < b.id ? 1 : -1;
  return 0;
}

/**
 * Layers the realtime overlay onto a freshly resolved server page: overlay
 * versions replace their REST twins on every page (a realtime change always
 * wins over an older REST twin), while overlay-only INSERTED rows are
 * prepended in newest-first order on page 0 ONLY — later cursor pages keep
 * their exact keyset semantics and are never reordered or injected into.
 * Overlay entries that are mere UPDATES (not inserts) never appear as extra
 * rows: they only refresh twins that the server page already contains.
 */
function mergeRealtimeOverlay(
  baseRows: Lead[],
  overlay: ReadonlyMap<string, Lead>,
  insertedIds: ReadonlySet<string>,
  isPageZero: boolean
): Lead[] {
  if (overlay.size === 0) return baseRows;
  const baseIds = new Set(baseRows.map((row) => row.id));
  const merged = baseRows.map((row) => overlay.get(row.id) ?? row);
  if (!isPageZero) return merged;
  const overlayOnly = [...overlay.entries()]
    .filter(([id]) => insertedIds.has(id) && !baseIds.has(id))
    .map(([, lead]) => lead)
    .sort(compareNewestFirst);
  return [...overlayOnly, ...merged];
}

export function useCrmLeadsPage(options: UseCrmLeadsPageOptions): UseCrmLeadsPageResult {
  const { getBearerToken } = options;

  // Filter edits debounce before they re-query: the RangePicker fires once per
  // bound, and each settled filter set starts a fresh cursor stack. pageIndex is
  // declared first so the debounce callback below can reset it without a
  // use-before-declare reference.
  const [pageIndex, setPageIndex] = useState(0);
  const [settledFilters, setSettledFilters] = useState(options.filters);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      // A settled filter set starts a fresh cursor stack at page 0. The reset
      // lives here (in the deferred callback) rather than synchronously in the
      // fetch effect body, which would cascade renders (react-hooks/
      // set-state-in-effect). pageSize changes reset via changePageSize.
      setSettledFilters(options.filters);
      setPageIndex(0);
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // options.filters is a stable composite from the caller's state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.filters]);

  const [pageSize, setPageSize] = useState(options.initialPageSize ?? CRM_LEADS_DEFAULT_PAGE_SIZE);
  const [rows, setRows] = useState<Lead[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<CrmLeadsPageError | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Start cursor of every visited page (index 0 = the very first page). Only
  // grows via server responses; the FE never decodes or fabricates cursors.
  const cursorStackRef = useRef<(string | null)[]>([null]);
  const sequenceRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const settledKeyRef = useRef<string | null>(null);

  // Realtime overlay state (module docblock has the contract): ids from the
  // historical first snapshot, every realtime id ever observed (dedupe), the
  // overlay rows themselves, and which of them were INSERTED by realtime (as
  // opposed to updated twins). The overlay maps are written synchronously so
  // a resolving REST response can never clobber a newer realtime change.
  const realtimeBaselineRef = useRef<Set<string> | null>(null);
  const seenRealtimeIdsRef = useRef<Set<string>>(new Set());
  const realtimeOverlayRef = useRef<Map<string, Lead>>(new Map());
  const realtimeInsertedIdsRef = useRef<Set<string>>(new Set());
  const realtimeRefreshTimerRef = useRef<number | null>(null);
  const settledFiltersRef = useRef(settledFilters);
  const pageIndexRef = useRef(pageIndex);

  useEffect(() => {
    settledFiltersRef.current = settledFilters;
    pageIndexRef.current = pageIndex;
  }, [settledFilters, pageIndex]);

  // A pending coalesced refresh must never fire after unmount.
  useEffect(
    () => () => {
      if (realtimeRefreshTimerRef.current !== null) {
        window.clearTimeout(realtimeRefreshTimerRef.current);
      }
    },
    []
  );

  const reconcileRealtimeSnapshot = useCallback((incoming: readonly Lead[]) => {
    const baseline = realtimeBaselineRef.current;
    const isFirstSnapshot = baseline === null;
    if (isFirstSnapshot) {
      // The stream's first snapshot after mount (or a filter reset) is the
      // historical baseline: record ids, never insert — otherwise the whole
      // Firestore history would mass-prepend over the server-paginated page.
      realtimeBaselineRef.current = new Set(incoming.map((lead) => lead.id));
    }
    const filters = settledFiltersRef.current;
    const hasReengageWindow =
      filters.reengageFromIsoDate !== null || filters.reengageToIsoDate !== null;
    const matchesProvableFilters = (lead: Lead): boolean =>
      (filters.projectKey === null || lead.projectKey === filters.projectKey) &&
      (filters.status === null || lead.workflowStatus === filters.status);

    let overlayChanged = false;
    let shouldScheduleRefresh = false;
    for (const lead of incoming) {
      if (isFirstSnapshot) continue;
      const knownBefore =
        realtimeBaselineRef.current !== null &&
        (realtimeBaselineRef.current.has(lead.id) ||
          seenRealtimeIdsRef.current.has(lead.id));
      seenRealtimeIdsRef.current.add(lead.id);
      if (knownBefore) {
        // Update twin: recorded in the overlay so the newest realtime state
        // wins over the REST twin on any page and on later refetches.
        realtimeOverlayRef.current.set(lead.id, lead);
        overlayChanged = true;
        continue;
      }
      if (!matchesProvableFilters(lead)) {
        // Client-side provable filter mismatch (project/status): the row does
        // not belong on this page; the seen set keeps it from re-processing.
        continue;
      }
      if (hasReengageWindow) {
        // The FE cannot prove a calendar-day match (Asia/Ho_Chi_Minh is
        // resolved by the backend): coalesce into one page-0 refetch instead
        // of guessing an insert.
        shouldScheduleRefresh = true;
        continue;
      }
      if (pageIndexRef.current !== 0) {
        // Keyset integrity: never inject an unseen newest row into a later
        // cursor page; returning to page 0 refetches and surfaces it.
        continue;
      }
      realtimeOverlayRef.current.set(lead.id, lead);
      realtimeInsertedIdsRef.current.add(lead.id);
      overlayChanged = true;
    }

    if (overlayChanged) {
      setRows((current) =>
        mergeRealtimeOverlay(
          current,
          realtimeOverlayRef.current,
          realtimeInsertedIdsRef.current,
          pageIndexRef.current === 0
        )
      );
    }
    if (shouldScheduleRefresh && realtimeRefreshTimerRef.current === null) {
      realtimeRefreshTimerRef.current = window.setTimeout(() => {
        realtimeRefreshTimerRef.current = null;
        // The refetch response is authoritative for the date window: drop the
        // overlay so it re-baselines instead of compounding guesses.
        realtimeOverlayRef.current = new Map();
        realtimeInsertedIdsRef.current = new Set();
        setReloadNonce((nonce) => nonce + 1);
      }, REALTIME_REFRESH_COALESCE_MS);
    }
  }, []);

  const goToPage = useCallback((target: number) => {
    setPageIndex((current) => {
      if (target === current || target < 0) {
        return current;
      }
      // Forward navigation is always accepted: the pager (LeadTable's stable
      // `total`) only ever offers the page immediately after the current one,
      // whose cursor is primed by the last successful fetch. Requiring a primed
      // cursor here used to silently drop the click whenever the cursor stack
      // had just been reset (filter/pageSize change) or the next cursor had not
      // resolved yet, so "Next" did nothing.
      if (target > current) {
        return target;
      }
      // Backward navigation must land on a page we actually visited, i.e. one
      // whose start cursor is recorded in the stack; otherwise we would fetch
      // with an undefined cursor and the backend would return the first page.
      if (cursorStackRef.current[target] === undefined) {
        return current;
      }
      return target;
    });
  }, []);

  const changePageSize = useCallback((nextPageSize: number) => {
    setPageSize((current) => (nextPageSize === current ? current : nextPageSize));
    // A new page size invalidates every cursor, so restart at page 0. Reset it
    // here (an event handler) rather than inside the fetch effect, which keeps
    // the effect free of synchronous state writes.
    setPageIndex(0);
  }, []);

  const retry = useCallback(() => setReloadNonce((nonce) => nonce + 1), []);

  useEffect(() => {
    // A changed filter set or page size invalidates every cursor: restart with a
    // fresh stack. The page index is already reset to 0 by the filter debounce
    // and by changePageSize, in the same state batch that changes settledKey, so
    // this effect never observes a stale non-zero page and needs no synchronous
    // setPageIndex (which would trip react-hooks/set-state-in-effect).
    const settledKey = JSON.stringify(settledFilters) + "|" + String(pageSize);
    if (settledKeyRef.current !== settledKey) {
      settledKeyRef.current = settledKey;
      cursorStackRef.current = [null];
      // A new filter window re-baselines the realtime overlay: previously
      // inserted rows may not match the new filters, and the next stream
      // snapshot is the historical baseline again. Cancel any pending
      // coalesced refresh — the filter refetch below already re-queries.
      realtimeBaselineRef.current = null;
      seenRealtimeIdsRef.current = new Set();
      realtimeOverlayRef.current = new Map();
      realtimeInsertedIdsRef.current = new Set();
      if (realtimeRefreshTimerRef.current !== null) {
        window.clearTimeout(realtimeRefreshTimerRef.current);
        realtimeRefreshTimerRef.current = null;
      }
    }

    const sequence = ++sequenceRef.current;
    const controller = new AbortController();
    abortRef.current = controller;

    void (async () => {
      // Flip to the loading state as the first action of the async task. This
      // still runs synchronously in the effect's tick (before the first await),
      // so the spinner appears immediately, but it is not a direct setState in
      // the effect body — which the React Compiler flags as a cascading render
      // (react-hooks/set-state-in-effect).
      setIsLoading(true);
      setError(null);
      try {
        const bearerToken = await getBearerToken();
        if (sequenceRef.current !== sequence) return;
        if (bearerToken === null) throw AUTH_ERROR;
        const page = await fetchCrmLeadsPage({
          projectKey: settledFilters.projectKey,
          status: settledFilters.status,
          reengageFromIsoDate: settledFilters.reengageFromIsoDate,
          reengageToIsoDate: settledFilters.reengageToIsoDate,
          cursor: cursorStackRef.current[pageIndex] ?? null,
          limit: pageSize,
          bearerToken,
          signal: controller.signal,
        });
        if (sequenceRef.current !== sequence) return;
        setRows(
          mergeRealtimeOverlay(
            page.rows,
            realtimeOverlayRef.current,
            realtimeInsertedIdsRef.current,
            pageIndex === 0
          )
        );
        setHasMore(page.hasMore);
        if (page.nextCursor !== null) {
          cursorStackRef.current[pageIndex + 1] = page.nextCursor;
        }
      } catch (cause: unknown) {
        if (sequenceRef.current !== sequence) return;
        // AbortError only happens when a newer request superseded this one.
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        const pageError = cause === AUTH_ERROR ? AUTH_ERROR : toPageError(cause);
        setError(pageError);
        // Never keep stale rows behind an auth wall — that would pretend the
        // user still has data access.
        if (pageError.isAuthError) {
          setRows([]);
          setHasMore(false);
        }
      } finally {
        if (sequenceRef.current === sequence) setIsLoading(false);
      }
    })();

    return () => {
      controller.abort();
    };
    // getBearerToken is a stable callback from the composition root.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settledFilters, pageSize, pageIndex, reloadNonce, getBearerToken]);

  return {
    rows,
    pageIndex,
    pageSize,
    hasNextPage: hasMore,
    hasPreviousPage: pageIndex > 0,
    isLoading,
    error,
    goToPage,
    changePageSize,
    retry,
    reconcileRealtimeSnapshot,
  };
}
