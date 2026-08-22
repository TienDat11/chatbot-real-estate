/**
 * crmApiClient — thin authorized fetch helpers for the CRM backend endpoints
 * (story 9.3). Every call carries `Authorization: Bearer <Firebase ID token>`
 * (getFreshIdToken) because the FastAPI CRM routes authenticate via Firebase
 * JWKS. Kept separate from lib/api.ts (public chat endpoints, no auth header)
 * on purpose; do not merge them.
 */

/** Row of a customer's lead history as returned by the search endpoint. */
export interface CrmCustomerLeadRow {
  id: string;
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
const crmCustomerPhoneEndpoint = (customerId: string) =>
  `/api/crm/customers/${encodeURIComponent(customerId)}/phone`;
const crmLeadStatusEndpoint = (leadId: string) =>
  `/api/crm/leads/${encodeURIComponent(leadId)}/status`;
const crmWithdrawMarketingConsentEndpoint = (customerId: string) =>
  `/api/crm/customers/${encodeURIComponent(customerId)}/withdraw-marketing-consent`;

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

/**
 * GET /api/crm/customers/search?phone=<raw> — resolves the opaque customer id
 * (and masked lead history) from a phone number the staff already holds.
 * Throws CrmApiClientError(404) when no customer matches.
 */
export async function searchCustomerByPhone(request: {
  rawPhone: string;
  bearerToken: string;
}): Promise<CrmCustomerProfile> {
  const response = await fetch(
    `${CRM_CUSTOMERS_SEARCH_ENDPOINT}?phone=${encodeURIComponent(request.rawPhone)}`,
    {
      headers: { ...authorizationHeader(request.bearerToken), Accept: "application/json" },
    }
  );
  if (!response.ok) {
    throw new CrmApiClientError(
      response.status,
      response.status === 404
        ? "Không tìm thấy khách hàng với số điện thoại này."
        : await readErrorMessage(response, "Tra cứu khách hàng thất bại.")
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
  bearerToken: string;
  status: string;
  rejectionReason?: string;
  /** ISO-8601 instant the sales schedules a re-contact for. */
  reengageAt?: string;
}): Promise<void> {
  const response = await fetch(crmLeadStatusEndpoint(request.leadId), {
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
