/**
 * Pure projections over a live lead snapshot (story 9.3): optimistic-status
 * merging and incoming-lead detection. Extracted from the hook so the tricky
 * parts are unit-testable without React.
 */
import type { Lead } from "@/domain/crm/lead";

/** Locally applied lead patch while a status PATCH is in flight. */
export type OptimisticLeadPatch = Partial<
  Pick<Lead, "workflowStatus" | "rejectionReason" | "reengageAt">
>;

export interface MergedLeadStreamProjection {
  /** Snapshot leads with still-relevant optimistic patches applied. */
  leads: Lead[];
  /** Patch keys whose authoritative snapshot state has caught up. */
  caughtUpLeadIds: string[];
}

/**
 * Merges optimistic patches over the authoritative snapshot. A patch is
 * dropped once the snapshot lead reflects its workflowStatus (the mirror has
 * caught up), so the stream stays the single source of truth long-term.
 */
export function mergeLeadsWithOptimisticPatches(
  snapshotLeads: readonly Lead[],
  optimisticPatches: ReadonlyMap<string, OptimisticLeadPatch>
): MergedLeadStreamProjection {
  const caughtUpLeadIds: string[] = [];
  const leads = snapshotLeads.map((lead) => {
    const patch = optimisticPatches.get(lead.id);
    if (!patch) {
      return lead;
    }
    if (
      patch.workflowStatus !== undefined &&
      lead.workflowStatus === patch.workflowStatus
    ) {
      caughtUpLeadIds.push(lead.id);
      return lead;
    }
    return { ...lead, ...patch };
  });
  return { leads, caughtUpLeadIds };
}

/**
 * Returns the ids present in `leads` but not in `previousLeadIds` — the set
 * of documents that arrived on the wire since the previous snapshot. The
 * hook uses it to fire "lead mới" notifications (first snapshot establishes
 * the baseline, so it never notifies).
 */
export function collectIncomingLeadIds(
  previousLeadIds: ReadonlySet<string>,
  leads: readonly Lead[]
): string[] {
  return leads.filter((lead) => !previousLeadIds.has(lead.id)).map((lead) => lead.id);
}

/**
 * From a merged snapshot, picks the leads that should raise a toast: freshly
 * arrived (id in incomingIds) and still untouched (status "new").
 */
export function selectNotifiableIncomingLeads(
  leads: readonly Lead[],
  incomingLeadIds: readonly string[]
): Lead[] {
  const incomingIdSet = new Set(incomingLeadIds);
  return leads.filter(
    (lead) => incomingIdSet.has(lead.id) && lead.workflowStatus === "new"
  );
}
