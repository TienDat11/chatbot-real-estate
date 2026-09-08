/**
 * Pure lead-filtering logic for the CRM table (story 9.3). No React, no
 * network — fully unit-testable projections over DOMAIN Lead entities.
 */
import type { Lead, LeadWorkflowStatus } from "@/domain/crm/lead";

/**
 * Date-window criteria for the leads toolbar (RejectedFilter.tsx). The window
 * is always active and applies to every status; it filters on reengage_at
 * (the re-contact schedule), never on created_at.
 */
export interface LeadToolbarFilterCriteria {
  /** Inclusive ISO calendar-date window on reengage_at; null = unbounded. */
  reengageWindowFromIsoDate: string | null;
  reengageWindowToIsoDate: string | null;
}

/** Criteria with nothing set — every lead matches the window. */
export const EMPTY_LEAD_TOOLBAR_FILTER: LeadToolbarFilterCriteria = {
  reengageWindowFromIsoDate: null,
  reengageWindowToIsoDate: null,
};

/** Calendar date (YYYY-MM-DD) of an ISO-8601 instant, or null when absent. */
function isoCalendarDateOf(isoInstant: string | null): string | null {
  return isoInstant !== null ? isoInstant.slice(0, 10) : null;
}

/** True when the lead's reengage_at day falls inside the window (inclusive). */
export function matchesReengageWindow(
  lead: Lead,
  criteria: LeadToolbarFilterCriteria
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

/**
 * Leaves only leads whose reengage_at falls inside the toolbar window. An
 * unbounded window keeps everything; a requested window drops leads without a
 * re-contact schedule. Status-independent by design — compose with
 * filterLeadsByWorkflowStatus for the full toolbar chain.
 */
export function filterLeadsByReengageWindow(
  leads: readonly Lead[],
  criteria: LeadToolbarFilterCriteria
): Lead[] {
  return leads.filter((lead) => matchesReengageWindow(lead, criteria));
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
