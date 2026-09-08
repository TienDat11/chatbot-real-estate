/**
 * crmApiClient — thin authorized fetch helpers for the CRM backend endpoints
 * (story 9.3, server-paged leads listing FR-31). Every call carries
 * `Authorization: Bearer <Firebase ID token>` (getFreshIdToken) because the
 * FastAPI CRM routes authenticate via Firebase JWKS. Kept separate from
 * lib/api.ts (public chat endpoints, no auth header) on purpose; do not merge.
 */

import type { Lead } from "@/domain/crm/lead";
import type { CrmLeadsPageResult } from "@/domain/crm/leadPage";

/** Row of a customer's lead history as returned by the search endpoint. */
export interface CrmCustomerLeadRow {
  id: string;
  /** Numeric Postgres leads.id mirrored by the backend's integer-only routes. */
  lead_id?: number;
  project_key: string;
  name: string | null;
  /** Pre-masked display phone; the full phone needs the reveal endpoint. */
  masked_phone: string | null;
  status: string;
  created_at: string | null;
}

/** Customer profile resolved from a raw phone number. */
export interface CrmCustomerProfile {
  /** Opaque backend id (HMAC of the phone) used by the reveal/consent routes. */
  customerId: string;
  leads: CrmCustomerLeadRow[];
}

/** One transcript turn of the chat session linked to a lead. */
export interface LeadConversationMessage {
  role: "user" | "assistant";
  content: string;
  /**
   * Assistant answer metadata (sources / images / lead_cta_hint); user turns
   * carry null. Kept loose on purpose — the panel reads only `sources`.
   */
  meta: Record<string, unknown> | null;
  created_at: string;
}

/** One row of GET /api/crm/leads (masked-phone summary; never the raw phone). */
export interface CrmLeadsPageItem {
  id: string;
  lead_id: number;
  project_key: string | null;
  display_name: string | null;
  masked_phone: string;
  lead_status: string;
  assigned_sales_id: number | null;
  created_at: string;
  rejection_reason: string | null;
  reengage_at: string | null;
  consent_service: boolean | null;
  consent_marketing: boolean | null;
  marketing_consent_withdrawn_at: string | null;
}

/** Transcript payload of GET /api/crm/leads/{lead_id}/conversation. */
export interface LeadConversation {
  sessionId: string | null;
  messages: LeadConversationMessage[];
}

/** Typed CRM API failure carrying the HTTP status for UX branching. */
export class CrmApiClientError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "CrmApiClientError";
    this.status = status;
  }
}

const CRM_CUSTOMERS_SEARCH_ENDPOINT = "/api/crm/customers/search";
const CRM_LEADS_PAGE_ENDPOINT = "/api/crm/leads";

export async function notifyCallStarted(request: { leadId: number; bearerToken: string }): Promise<{ customerHasRegisteredDevices: boolean }> {
  const response = await fetch("/api/notifications/call-started", {
    method: "POST",
    headers: { ...authorizationHeader(request.bearerToken), "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ lead_id: request.leadId }),
  });
  if (!response.ok) throw new CrmApiClientError(response.status, "Không thể gửi thông báo cuộc gọi.");
  const body = (await response.json()) as { customer_has_device?: unknown; customer_has_registered_devices?: unknown; has_registered_devices?: unknown };
  return { customerHasRegisteredDevices: body.customer_has_device === true || body.customer_has_registered_devices === true || body.has_registered_devices === true };
}
const crmCustomerPhoneEndpoint = (customerId: string) =>
  `/api/crm/customers/${encodeURIComponent(customerId)}/phone`;
/**
 * Builds the path segment required by the integer-only lead routes.
 * Legacy opaque document ids are customer identifiers, not lead identifiers,
 * so they must never be sent to these endpoints.
 */
function leadEndpointId(numericLeadId: number | undefined): string {
  if (
    numericLeadId === undefined ||
    !Number.isSafeInteger(numericLeadId) ||
    numericLeadId <= 0
  ) {
    throw new CrmApiClientError(400, "Không xác định được mã lead hợp lệ.");
  }
  return String(numericLeadId);
}
const crmLeadStatusEndpoint = (numericLeadId: number | undefined) =>
  `/api/crm/leads/${leadEndpointId(numericLeadId)}/status`;
const crmWithdrawMarketingConsentEndpoint = (customerId: string) =>
  `/api/crm/customers/${encodeURIComponent(customerId)}/withdraw-marketing-consent`;
const crmLeadConversationEndpoint = (numericLeadId: number | undefined) =>
  `/api/crm/leads/${leadEndpointId(numericLeadId)}/conversation`;
function authorizationHeader(bearerToken: string): Record<string, string> {
  return { Authorization: `Bearer ${bearerToken}` };
}

async function readErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // Non-JSON error body; the fallback message below is enough.
  }
  return fallback;
}

/** Normalize the same phone separators accepted by the lead form. */
function normalizeCustomerSearchPhone(rawPhone: string): string {
  return rawPhone.replace(/[\s,.-]+/g, "");
}

/**
 * Maps one wire summary onto the DOMAIN Lead entity so the table and the
 * CustomerDetail drawer keep a single shape. Fields the REST summary does not
 * carry (note, budget, escalation counters) keep the domain's neutral values;
 * the drawer never sourced them from this listing anyway.
 */
export function mapLeadsPageItemToLead(item: CrmLeadsPageItem): Lead {
  return {
    id: item.id,
    // The REST summary's `id` IS the backend customer HMAC (see the route's
    // CrmLeadSummary contract note), the exact identity the reveal/consent
    // routes authorize against. Carrying it as `customerId` lets the drawer
    // direct-reveal an owner-scoped listing row without the manual phone
    // lookup; the sales listing is server-scoped to the caller's own leads.
    customerId: item.id,
    leadId: item.lead_id,
    projectKey: item.project_key ?? "",
    deviceId: null,
    name: item.display_name,
    maskedPhone: item.masked_phone,
    note: null,
    budgetVnd: null,
    consentFlags: {
      consentService: item.consent_service ?? false,
      consentMarketing: item.consent_marketing ?? false,
    },
    workflowStatus: item.lead_status as Lead["workflowStatus"],
    assignedSalesId: item.assigned_sales_id,
    assignedSalesFirebaseUid: null,
    rejectionReason: item.rejection_reason,
    reengageAt: item.reengage_at,
    marketingWithdrawnAt: item.marketing_consent_withdrawn_at,
    escalCount: 0,
    createdAt: item.created_at,
    updatedAt: item.created_at,
    closedAt: null,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * GET /api/crm/leads — one keyset-paginated page (FR-31). `cursor` is the
 * opaque token the previous response returned; every filter is a query
 * parameter so a narrowed page is computed SERVER-side (no client get-all).
 * `signal` (AbortController) cancels a superseded request so a slow response
 * can never overwrite a newer page.
 */
export async function fetchCrmLeadsPage(request: {
  projectKey: string | null;
  status: string | null;
  reengageFromIsoDate: string | null;
  reengageToIsoDate: string | null;
  cursor: string | null;
  limit: number;
  bearerToken: string;
  signal: AbortSignal;
}): Promise<CrmLeadsPageResult> {
  const params = new URLSearchParams();
  if (request.projectKey !== null) params.set("project_key", request.projectKey);
  if (request.status !== null) params.set("status", request.status);
  if (request.reengageFromIsoDate !== null) params.set("reengage_from", request.reengageFromIsoDate);
  if (request.reengageToIsoDate !== null) params.set("reengage_to", request.reengageToIsoDate);
  if (request.cursor !== null) params.set("cursor", request.cursor);
  params.set("limit", String(request.limit));
  const response = await fetch(`${CRM_LEADS_PAGE_ENDPOINT}?${params.toString()}`, {
    headers: { ...authorizationHeader(request.bearerToken), Accept: "application/json" },
    signal: request.signal,
  });
  if (!response.ok) {
    throw new CrmApiClientError(
      response.status,
      await readErrorMessage(response, "Không tải được danh sách lead.")
    );
  }
  const body = (await response.json()) as {
    items?: unknown;
    next_cursor?: unknown;
    has_more?: unknown;
    server_time?: unknown;
  };
  if (!isRecord(body) || !Array.isArray(body.items) || typeof body.has_more !== "boolean") {
    throw new CrmApiClientError(502, "Phản hồi danh sách lead không hợp lệ.");
  }
  return {
    rows: (body.items as CrmLeadsPageItem[]).map(mapLeadsPageItemToLead),
    nextCursor: typeof body.next_cursor === "string" ? body.next_cursor : null,
    hasMore: body.has_more,
    serverTime: typeof body.server_time === "string" ? body.server_time : "",
  };
}

/**
 * POST /api/crm/customers/search — resolves the opaque customer id (and
 * masked lead history) from a normalized phone number in the JSON body.
 * Throws CrmApiClientError(404) when no customer matches.
 */
export async function searchCustomerByPhone(request: {
  rawPhone: string;
  bearerToken: string;
}): Promise<CrmCustomerProfile> {
  const response = await fetch(CRM_CUSTOMERS_SEARCH_ENDPOINT, {
    method: "POST",
    headers: {
      ...authorizationHeader(request.bearerToken),
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ phone: normalizeCustomerSearchPhone(request.rawPhone) }),
  });
  if (!response.ok) {
    throw new CrmApiClientError(
      response.status,
      response.status === 404
        ? "Không tìm thấy khách hàng với số điện thoại này."
        : "Tra cứu khách hàng thất bại."
    );
  }
  const body = (await response.json()) as {
    customer_id?: unknown;
    leads?: unknown;
  };
  if (typeof body.customer_id !== "string" || !Array.isArray(body.leads)) {
    throw new CrmApiClientError(502, "Phản hồi tra cứu khách hàng không hợp lệ.");
  }
  return { customerId: body.customer_id, leads: body.leads as CrmCustomerLeadRow[] };
}

/**
 * GET /api/crm/customers/{customer_id}/phone — reveals the full phone. The
 * backend allows only the assigning sales or an admin; 403 is surfaced as a
 * typed error so the drawer can explain the denial.
 */
export async function fetchRevealedPhoneNumber(request: {
  customerId: string;
  bearerToken: string;
}): Promise<string> {
  const response = await fetch(crmCustomerPhoneEndpoint(request.customerId), {
    headers: { ...authorizationHeader(request.bearerToken), Accept: "application/json" },
  });
  if (!response.ok) {
    throw new CrmApiClientError(
      response.status,
      response.status === 403
        ? "Bạn không có quyền xem số điện thoại đầy đủ của khách hàng này."
        : await readErrorMessage(response, "Không xem được số điện thoại.")
    );
  }
  const body = (await response.json()) as { phone?: unknown };
  if (typeof body.phone !== "string") {
    throw new CrmApiClientError(502, "Phản hồi số điện thoại không hợp lệ.");
  }
  return body.phone;
}

/**
 * PATCH /api/crm/leads/{leadId}/status — updates the workflow status, with
 * the rejection companions (reason + optional reengage instant) when the
 * status is "lost" (the domain's rejection state). snake_case body mirrors
 * the FastAPI model.
 */
export async function updateLeadStatus(request: {
  leadId: string;
  /** Numeric Postgres leads.id required by the integer-only backend route. */
  numericLeadId?: number;
  bearerToken: string;
  status: string;
  rejectionReason?: string;
  /** ISO-8601 instant the sales schedules a re-contact for. */
  reengageAt?: string;
}): Promise<void> {
  const response = await fetch(
    crmLeadStatusEndpoint(request.numericLeadId),
    {
      method: "PATCH",
      headers: {
        ...authorizationHeader(request.bearerToken),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
      status: request.status,
      ...(request.rejectionReason !== undefined
        ? { rejection_reason: request.rejectionReason }
        : {}),
      ...(request.reengageAt !== undefined ? { reengage_at: request.reengageAt } : {}),
    }),
  });
  if (!response.ok) {
    throw new CrmApiClientError(
      response.status,
      await readErrorMessage(response, "Cập nhật trạng thái thất bại.")
    );
  }
}

/**
 * POST /api/crm/customers/{customerId}/withdraw-marketing-consent — the
 * danger action: permanently stops marketing contact for the customer.
 */
export async function withdrawMarketingConsent(request: {
  customerId: string;
  bearerToken: string;
}): Promise<void> {
  const response = await fetch(crmWithdrawMarketingConsentEndpoint(request.customerId), {
    method: "POST",
    headers: { ...authorizationHeader(request.bearerToken), Accept: "application/json" },
  });
  if (!response.ok) {
    throw new CrmApiClientError(
      response.status,
      await readErrorMessage(response, "Ngừng liên hệ thất bại.")
    );
  }
}

/**
 * GET /api/crm/leads/{leadId}/conversation — the chat transcript of the session
 * linked to this lead (set when the customer submitted their phone from chat).
 * The backend answers 404 "Conversation not found" for a lead that never had a
 * linked chat; that is the drawer's friendly empty state, so it resolves to an
 * empty transcript instead of throwing.
 */
export async function fetchLeadConversation(request: {
  leadId: string;
  /** Numeric Postgres leads.id required by the integer-only backend route. */
  numericLeadId?: number;
  bearerToken: string;
}): Promise<LeadConversation> {
  const response = await fetch(
    crmLeadConversationEndpoint(request.numericLeadId),
    {
      headers: {
        ...authorizationHeader(request.bearerToken),
        Accept: "application/json",
      },
    }
  );
  if (!response.ok) {
    if (response.status === 404) {
      return { sessionId: null, messages: [] };
    }
    throw new CrmApiClientError(
      response.status,
      await readErrorMessage(response, "Không tải được hội thoại với khách.")
    );
  }
  const body = (await response.json()) as {
    session_id?: unknown;
    messages?: unknown;
  };
  if (!Array.isArray(body.messages)) {
    throw new CrmApiClientError(502, "Phản hồi hội thoại không hợp lệ.");
  }
  return {
    sessionId: typeof body.session_id === "string" ? body.session_id : null,
    messages: body.messages as LeadConversationMessage[],
  };
}
