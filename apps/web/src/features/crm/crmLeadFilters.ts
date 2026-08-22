/**
 * Pure lead-filtering logic for the CRM table (story 9.3). No React, no
 * network — fully unit-testable projections over DOMAIN Lead entities.
 */
import type { Lead, LeadWorkflowStatus } from "@/domain/crm/lead";

/** Filter criteria for the rejected-leads toolbar (RejectedFilter.tsx). */
export interface RejectedLeadsFilterCriteria {
  /** Exact project key, or null for "every subscribed project". */
  projectKey: string | null;
  /** Case-insensitive substring on the stored rejection reason; null = any. */
  rejectionReason: string | null;
  /** Inclusive ISO calendar-date window on reengage_at; null = unbounded. */
  reengageWindowFromIsoDate: string | null;
  reengageWindowToIsoDate: string | null;
}

/** Criteria with nothing set — every rejected lead matches. */
export const EMPTY_REJECTED_LEADS_FILTER: RejectedLeadsFilterCriteria = {
  projectKey: null,
  rejectionReason: null,
  reengageWindowFromIsoDate: null,
  reengageWindowToIsoDate: null,
};

/** Calendar date (YYYY-MM-DD) of an ISO-8601 instant, or null when absent. */
function isoCalendarDateOf(isoInstant: string | null): string | null {
  return isoInstant !== null ? isoInstant.slice(0, 10) : null;
}

/** True when the lead's reengage_at day falls inside the window (inclusive). */
function matchesReengageWindow(
  lead: Lead,
  criteria: RejectedLeadsFilterCriteria
): boolean {
  const windowUnbounded =
    criteria.reengageWindowFromIsoDate === null && criteria.reengageWindowToIsoDate === null;
  if (windowUnbounded) {
    return true;
  }
  const reengageDay = isoCalendarDateOf(lead.reengageAt);
  if (reengageDay === null) {
    // A window was requested but the lead has no re-contact schedule.
    return false;
  }
  if (
    criteria.reengageWindowFromIsoDate !== null &&
    reengageDay < criteria.reengageWindowFromIsoDate
  ) {
    return false;
  }
  if (
    criteria.reengageWindowToIsoDate !== null &&
    reengageDay > criteria.reengageWindowToIsoDate
  ) {
    return false;
  }
  return true;
}

/** True when every non-null criterion matches the rejected lead. */
export function matchesRejectedLeadsFilter(
  lead: Lead,
  criteria: RejectedLeadsFilterCriteria
): boolean {
  // "lost" is the domain's rejection state (PG CHECK vocabulary); a lost lead
  // without a stored reason is still shown in the rejected view.
  if (lead.workflowStatus !== "lost") {
    return false;
  }
  if (criteria.projectKey !== null && lead.projectKey !== criteria.projectKey) {
    return false;
  }
  if (criteria.rejectionReason !== null && criteria.rejectionReason.trim() !== "") {
    const storedReason = (lead.rejectionReason ?? "").toLowerCase();
    if (!storedReason.includes(criteria.rejectionReason.trim().toLowerCase())) {
      return false;
    }
  }
  return matchesReengageWindow(lead, criteria);
}

/** Leaves only rejected leads matching the toolbar criteria. */
export function filterRejectedLeads(
  leads: readonly Lead[],
  criteria: RejectedLeadsFilterCriteria
): Lead[] {
  return leads.filter((lead) => matchesRejectedLeadsFilter(lead, criteria));
}

/** Leaves only leads in the given workflow status; null keeps everything. */
export function filterLeadsByWorkflowStatus(
  leads: readonly Lead[],
  status: LeadWorkflowStatus | null
): Lead[] {
  return status === null
    ? [...leads]
    : leads.filter((lead) => lead.workflowStatus === status);
}

/**
 * Groups the leads that belong to the same customer as the anchor lead.
 * Customer identity is FE-side derived from the masked phone (the raw phone
 * never leaves the backend); a null mask groups nothing.
 */
export function selectCustomerLeadsByMaskedPhone(
  leads: readonly Lead[],
  anchorLead: Lead
): Lead[] {
  if (anchorLead.maskedPhone === null) {
    return [anchorLead];
  }
  return leads.filter((lead) => lead.maskedPhone === anchorLead.maskedPhone);
}
