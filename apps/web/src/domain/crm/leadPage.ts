/**
 * Server-paginated CRM leads page types (plan FE-CRM-PAGING / FR-31).
 *
 * Pure TypeScript: no Firebase, no infrastructure imports. A page row is the
 * DOMAIN Lead entity — the client maps the wire summary (masked-phone view
 * only) onto it so the table and drawer keep a single shape. The backend
 * returns an opaque keyset cursor; the FE never decodes it.
 */
import type { Lead, LeadWorkflowStatus } from "./lead";

/** Server-side filters mirroring GET /api/crm/leads query parameters. */
export interface CrmLeadsPageFilters {
  /** Registry key narrowing the listing; null = every active project. */
  projectKey: string | null;
  /** Workflow status filter; null = Tất cả. */
  status: LeadWorkflowStatus | null;
  /**
   * Inclusive reengage calendar date (YYYY-MM-DD); null = unbounded. The
   * backend interprets the calendar day in Asia/Ho_Chi_Minh (contract C1) —
   * the FE never shifts these dates itself.
   */
  reengageFromIsoDate: string | null;
  reengageToIsoDate: string | null;
}

/** One resolved server page: rows plus the keyset continuation token. */
export interface CrmLeadsPageResult {
  rows: Lead[];
  /** Opaque cursor starting the NEXT page; null on the final page. */
  nextCursor: string | null;
  hasMore: boolean;
  /** Backend server_time (ISO instant) for observability, not for paging. */
  serverTime: string;
}
