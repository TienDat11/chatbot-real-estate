"use client";

/**
 * useCrmLeadStream — CRM-facing realtime lead stream (story 9.3).
 *
 * ONE subscription for the whole workspace through LeadRealtimeService
 * (application layer, port-typed): a sales streams only their assigned leads
 * (the Firestore rules require the where clause), an admin streams everything.
 * Project narrowing happens client-side after the fact — the wire scope is
 * the sales isolation key, not the project.
 * Optimistic patches cover status PATCH latency and self-release once the
 * mirror catches up.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Lead } from "@/domain/crm/lead";
import type {
  RealtimeChannelError,
  RealtimeConnectionState,
} from "@/domain/realtime/realtimeSubscription";
import { useRealtimeContainer } from "@/lib/realtime/useRealtimeContainer";
import {
  collectIncomingLeadIds,
  mergeLeadsWithOptimisticPatches,
  selectNotifiableIncomingLeads,
  type OptimisticLeadPatch,
} from "./leadStreamProjection";

export interface UseCrmLeadStreamOptions {
  /**
   * Per-sales Firestore isolation key (sales role: their auth uid; admin:
   * null). Becomes where(assigned_sales_firebase_uid == uid) on the wire.
   */
  assignedSalesFirebaseUidFilter: string | null;
  /** Fired for every lead that arrives live with status "new". */
  onIncomingLead?: (lead: Lead) => void;
}

export interface UseCrmLeadStreamResult {
  /** Authoritative snapshot newest-first, with optimistic patches applied. */
  leads: Lead[];
  connectionState: RealtimeConnectionState;
  error: RealtimeChannelError | null;
  /** Applies a local status patch while a PATCH request is in flight. */
  applyOptimisticLeadPatch: (leadId: string, patch: OptimisticLeadPatch) => void;
  /** Removes a patch (e.g. the PATCH failed and the UI must fall back). */
  clearOptimisticLeadPatch: (leadId: string) => void;
}

export function useCrmLeadStream(
  options: UseCrmLeadStreamOptions
): UseCrmLeadStreamResult {
  const { leadRealtimeService } = useRealtimeContainer();
  const assignedSalesFirebaseUidFilter = options.assignedSalesFirebaseUidFilter;

  // Keep the callback in a ref so changing it never resubscribes the stream.
  const onIncomingLeadRef = useRef(options.onIncomingLead);
  onIncomingLeadRef.current = options.onIncomingLead;

  const [snapshotLeads, setSnapshotLeads] = useState<readonly Lead[]>([]);
  const [connectionState, setConnectionState] =
    useState<RealtimeConnectionState>("connecting");
  const [error, setError] = useState<RealtimeChannelError | null>(null);
  const [optimisticPatches, setOptimisticPatches] = useState<
    ReadonlyMap<string, OptimisticLeadPatch>
  >(new Map());

  useEffect(() => {
    setSnapshotLeads([]);
    setError(null);
    setConnectionState("connecting");

    let seenLeadIds = new Set<string>();
    let firstSnapshot = true;
    const handle = leadRealtimeService.streamLeadsForCrm(
      { assignedSalesFirebaseUid: assignedSalesFirebaseUidFilter },
      {
        onLeadsChanged: (leads) => {
          setConnectionState("active");
          setError(null);
          if (!firstSnapshot) {
            // Only leads that appeared since the previous snapshot are "live
            // arrivals"; the first snapshot is the page-load baseline.
            const incomingIds = collectIncomingLeadIds(seenLeadIds, leads);
            for (const lead of selectNotifiableIncomingLeads(leads, incomingIds)) {
              onIncomingLeadRef.current?.(lead);
            }
          }
          firstSnapshot = false;
          seenLeadIds = new Set(leads.map((lead) => lead.id));
          setSnapshotLeads(leads);
        },
        onConnectionStateChanged: setConnectionState,
        onError: setError,
      }
    );

    return () => {
      handle.unsubscribe();
    };
  }, [leadRealtimeService, assignedSalesFirebaseUidFilter]);

  const sortedSnapshot = useMemo(() => {
    // Newest first; ISO-8601 strings sort chronologically, equal instants
    // keep wire order (consistent comparator, stable sort).
    return [...snapshotLeads].sort((a, b) => {
      if (a.createdAt === b.createdAt) {
        return 0;
      }
      return a.createdAt < b.createdAt ? 1 : -1;
    });
  }, [snapshotLeads]);

  const projection = useMemo(
    () => mergeLeadsWithOptimisticPatches(sortedSnapshot, optimisticPatches),
    [sortedSnapshot, optimisticPatches]
  );

  // Release patches the snapshot has confirmed, so long-term state is purely
  // authoritative. Runs after render projection, keeping it dependency-free.
  useEffect(() => {
    if (projection.caughtUpLeadIds.length === 0) {
      return;
    }
    setOptimisticPatches((previous) => {
      const next = new Map(previous);
      for (const leadId of projection.caughtUpLeadIds) {
        next.delete(leadId);
      }
      return next;
    });
  }, [projection.caughtUpLeadIds]);

  const applyOptimisticLeadPatch = useCallback(
    (leadId: string, patch: OptimisticLeadPatch) => {
      setOptimisticPatches((previous) => {
        const next = new Map(previous);
        next.set(leadId, patch);
        return next;
      });
    },
    []
  );

  const clearOptimisticLeadPatch = useCallback((leadId: string) => {
    setOptimisticPatches((previous) => {
      if (!previous.has(leadId)) {
        return previous;
      }
      const next = new Map(previous);
      next.delete(leadId);
      return next;
    });
  }, []);

  return {
    leads: projection.leads,
    connectionState,
    error,
    applyOptimisticLeadPatch,
    clearOptimisticLeadPatch,
  };
}
