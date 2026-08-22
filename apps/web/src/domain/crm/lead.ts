/**
 * Domain entity for a customer lead in the realtime/CRM layer.
 *
 * This file is PURE TypeScript: it must never import from "firebase/*" nor from
 * infrastructure/. All temporal fields are ISO-8601 strings (never Firestore
 * Timestamp objects), and all contact fields are pre-masked — the FE never
 * processes a raw phone number (the backend computes the opaque customer_id and
 * the masked display phone).
 */

/** Workflow states produced by the backend lead-routing state machine. */
export const LEAD_WORKFLOW_STATUSES = [
  "new",
  "assigned",
  "called",
  "callback",
  "no_answer",
  "booked",
  "lost",
  "expired",
] as const;

/** Mirrors the `status` CHECK constraint on db/lead_schema.sql. */
export type LeadWorkflowStatus = (typeof LEAD_WORKFLOW_STATUSES)[number];

/**
 * Explicit customer consent split by purpose. The backend lead submission only
 * carries a single `consent` boolean today; the Firestore mirror stores the
 * finer-grained flags so CRM actions can be gated per purpose without touching
 * the legacy API contract.
 */
export interface ConsentFlags {
  /** Consent to be contacted about this project's sales offer. */
  consentService: boolean;
  /** Consent to receive broader marketing communication. */
  consentMarketing: boolean;
}

/**
 * A lead as seen by the realtime/CRM UI.
 *
 * `id` is the opaque Firestore document id — an HMAC-SHA256 of the customer
 * phone, computed BACKEND-ONLY. The FE treats it as an opaque string and never
 * derives anything from it.
 */
export interface Lead {
  /** Opaque document id (backend-computed HMAC of the phone); never parsed. */
  id: string;
  /** Registry key of the project the lead belongs to (story 10.1, G1). */
  projectKey: string;
  /** Anonymous persistent device id (D7); PII once paired with a phone. */
  deviceId: string | null;
  name: string | null;
  /** Pre-masked contact display value; no raw phone handling in the FE. */
  maskedPhone: string | null;
  note: string | null;
  budgetVnd: number | null;
  consentFlags: ConsentFlags;
  workflowStatus: LeadWorkflowStatus;
  /** Postgres sales row id that owns the lead, when assigned. */
  assignedSalesId: number | null;
  escalCount: number;
  /** ISO-8601 instant the lead was first mirrored. */
  createdAt: string;
  /** ISO-8601 instant of the last mirrored change. */
  updatedAt: string;
  /** ISO-8601 instant the lead reached a terminal state, or null. */
  closedAt: string | null;
}
