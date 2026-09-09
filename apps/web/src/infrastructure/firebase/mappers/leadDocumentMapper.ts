/**
 * Lead document mapper — Firestore DTO (snake_case) <-> domain Lead (camelCase).
 *
 * The Firestore `leads` collection mirrors the backend Postgres LeadRow in
 * snake_case (see api/infrastructure/ports/leads.py). This mapper owns the
 * shape translation plus the Timestamp <-> ISO-8601 string conversion, so the
 * domain and application layers never see Firestore types.
 */
import {
  Timestamp,
  serverTimestamp,
  type DocumentData,
  type FieldValue,
} from "firebase/firestore";
import { LEAD_WORKFLOW_STATUSES } from "@/domain/crm/lead";
import type {
  ConsentFlags,
  Lead,
  LeadWorkflowStatus,
} from "@/domain/crm/lead";

/** Snake_case shape of a lead document as stored in Firestore. */
export interface LeadDocumentDto {
  id: string;
  /**
   * Opaque backend customer identity (HMAC of the phone) written by ADR-0004
   * mirrors; absent on documents mirrored before the field existed, whose
   * document id still was the customer digest.
   */
  customer_id?: string | null;
  /**
   * Numeric Postgres leads.id written by newer mirrors; absent on documents
   * mirrored before the field existed (read as undefined on the domain).
   */
  lead_id?: number | null;
  project_key: string;
  device_id: string | null;
  /**
   * Optional mirror of the backend display name; newer documents carry both
   * this and the legacy `name`, older ones carry only `name`.
   */
  display_name?: string | null;
  name: string | null;
  /** Pre-masked by the backend mirror writer; the FE never holds raw phones. */
  masked_phone: string | null;
  note: string | null;
  budget_vnd: number | null;
  consent_service: boolean;
  consent_marketing: boolean;
  /**
   * The backend mirror writes `lead_status`; `status` is kept as a legacy
   * read alias so documents written by older tooling still decode.
   */
  lead_status?: LeadWorkflowStatus;
  status: LeadWorkflowStatus;
  assigned_sales_id: number | null;
  /** Firebase uid of the assigned sales (per-sales rules isolation key). */
  assigned_sales_firebase_uid: string | null;
  rejection_reason: string | null;
  reengage_at: string | null;
  marketing_withdrawn_at: string | null;
  escal_count: number;
  /** serverTimestamp() sentinel on write; Timestamp (or ISO string) on read. */
  created_at: FieldValue | Timestamp | string | null;
  updated_at: FieldValue | Timestamp | string | null;
  closed_at: FieldValue | Timestamp | string | null;
}

/** Maps a domain Lead to the snake_case Firestore write payload. */
export function leadToLeadDocumentDto(lead: Lead): LeadDocumentDto {
  return {
    id: lead.id,
    project_key: lead.projectKey,
    device_id: lead.deviceId,
    name: lead.name,
    masked_phone: lead.maskedPhone,
    note: lead.note,
    budget_vnd: lead.budgetVnd,
    consent_service: lead.consentFlags.consentService,
    consent_marketing: lead.consentFlags.consentMarketing,
    lead_status: lead.workflowStatus,
    status: lead.workflowStatus,
    assigned_sales_id: lead.assignedSalesId,
    assigned_sales_firebase_uid: lead.assignedSalesFirebaseUid,
    rejection_reason: lead.rejectionReason,
    reengage_at: lead.reengageAt,
    marketing_withdrawn_at: lead.marketingWithdrawnAt,
    escal_count: lead.escalCount,
    // Written as serverTimestamp so Firestore stamps the authoritative time;
    // the FE domain keeps ISO-8601 strings and never fabricates timestamps.
    created_at: serverTimestamp(),
    updated_at: serverTimestamp(),
    closed_at: lead.closedAt ? serverTimestamp() : null,
  };
}

/** Maps a Firestore snapshot (DocumentData) to a domain Lead. */
export function mapLeadDocumentData(
  documentData: DocumentData,
  documentId: string
): Lead {
  return {
    id: documentId,
    // Opaque customer identity needed by the CRM reveal/consent routes; the
    // mirror carries it as a FIELD since ADR-0004 (the document id no longer
    // is the customer digest), and the drawer falls back to the document id
    // for legacy documents that predate the field.
    customerId: toOptionalCustomerId(documentData.customer_id),
    // Numeric Postgres leads.id needed by the integer-only CRM status/
    // conversation routes; without it crmApiClient's guard would reject every
    // drawer call with 400 even though the mirror document carries the id.
    leadId: toOptionalNumericLeadId(documentData.lead_id),
    projectKey: documentData.project_key,
    deviceId: documentData.device_id ?? null,
    // display_name is the mirrored canonical name; `name` stays as the legacy
    // alias so pre-display_name documents still decode (mirrors the
    // lead_status/status fallback pattern above). Same `??` convention as the
    // other nullable strings: only null/absent falls through, empty strings
    // are preserved verbatim.
    name:
      documentData.display_name ?? documentData.name ?? null,
    maskedPhone: documentData.masked_phone ?? null,
    note: documentData.note ?? null,
    budgetVnd: documentData.budget_vnd ?? null,
    consentFlags: {
      consentService: Boolean(documentData.consent_service),
      consentMarketing: Boolean(documentData.consent_marketing),
    },
    workflowStatus: toWorkflowStatusOrFallback(
      // Mirror documents key the status as lead_status; fall back to the
      // legacy alias so pre-mirror-era tooling documents still decode.
      documentData.lead_status ?? documentData.status
    ),
    assignedSalesId: documentData.assigned_sales_id ?? null,
    assignedSalesFirebaseUid: documentData.assigned_sales_firebase_uid ?? null,
    rejectionReason: documentData.rejection_reason ?? null,
    reengageAt: toIso8601StringOrNull(documentData.reengage_at),
    marketingWithdrawnAt: toIso8601StringOrNull(
      documentData.marketing_withdrawn_at
    ),
    escalCount: documentData.escal_count ?? 0,
    createdAt: toIso8601StringOrFallback(documentData.created_at),
    updatedAt: toIso8601StringOrFallback(documentData.updated_at),
    closedAt: toIso8601StringOrNull(documentData.closed_at),
  };
}

/**
 * Safe decode of the mirrored numeric lead id. Mirrors the crmApiClient guard
 * exactly (safe positive integer): documents written before the field existed
 * (absent) or malformed values decode as undefined instead of leaking an
 * unusable id into the domain — the API client stays the single place that
 * turns a missing id into a user-facing error.
 */
function toOptionalNumericLeadId(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    return undefined;
  }
  return value;
}

/**
 * Safe decode of the mirrored opaque customer_id (ADR-0004): documents
 * mirrored before the field existed (absent) or malformed values decode as
 * undefined instead of leaking a bogus identity — consumers then fall back to
 * the legacy document-id-as-customer-id contract.
 */
function toOptionalCustomerId(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) {
    return undefined;
  }
  return value;
}

/** Converts a Firestore Timestamp (or an already-stored ISO string) to ISO-8601. */
function toIso8601StringOrFallback(value: unknown): string {
  if (value instanceof Timestamp) {
    return value.toDate().toISOString();
  }
  if (typeof value === "string") {
    return value;
  }
  // Mirror rows written before the timestamp fields were populated should not
  // crash the stream; fall back to the Unix epoch so ordering stays stable.
  return new Date(0).toISOString();
}

/**
 * Safe decode of the workflow status. Mirror writes are controlled today, but
 * a malformed or legacy document must not crash the live stream: any value
 * outside the known status set falls back to "new" instead of propagating an
 * unvalidated cast.
 */
function toWorkflowStatusOrFallback(value: unknown): LeadWorkflowStatus {
  return LEAD_WORKFLOW_STATUSES.includes(value as LeadWorkflowStatus)
    ? (value as LeadWorkflowStatus)
    : "new";
}

/** Same conversion but null-safe for optional timestamps. */
function toIso8601StringOrNull(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  return toIso8601StringOrFallback(value);
}

/** Extracts the consent flags from a domain Lead for validation/display. */
export function consentFlagsOf(lead: Lead): ConsentFlags {
  return lead.consentFlags;
}
