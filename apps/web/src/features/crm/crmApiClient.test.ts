import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CrmApiClientError,
  fetchRevealedPhoneNumber,
  searchCustomerByPhone,
  updateLeadStatus,
  withdrawMarketingConsent,
} from "@/features/crm/crmApiClient";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>();
vi.stubGlobal("fetch", fetchMock);

afterEach(() => {
  fetchMock.mockReset();
});

describe("searchCustomerByPhone", () => {
  it("sends the bearer token and maps the snake_case response", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        customer_id: "customer-9",
        leads: [{ id: "row-1", project_key: "camellia", masked_phone: "090****456" }],
      })
    );
    const profile = await searchCustomerByPhone({
      rawPhone: "0901234567",
      bearerToken: "token-1",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/customers/search?phone=0901234567");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
    expect(profile.customerId).toBe("customer-9");
    expect(profile.leads[0]).toMatchObject({ id: "row-1", project_key: "camellia" });
  });

  it("throws a typed 404 error for unknown customers", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "not found" }, 404));
    await expect(
      searchCustomerByPhone({ rawPhone: "0999", bearerToken: "token-1" })
    ).rejects.toMatchObject({ name: "CrmApiClientError", status: 404 });
  });
});

describe("fetchRevealedPhoneNumber", () => {
  it("calls the reveal endpoint with the bearer token", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ phone: "0901234567" }));
    const phone = await fetchRevealedPhoneNumber({
      customerId: "customer-9",
      bearerToken: "token-1",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/customers/customer-9/phone");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
    expect(phone).toBe("0901234567");
  });

  it("surfaces a 403 as a CrmApiClientError", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "forbidden" }, 403));
    const error = await fetchRevealedPhoneNumber({
      customerId: "customer-9",
      bearerToken: "token-1",
    }).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(CrmApiClientError);
    expect((error as CrmApiClientError).status).toBe(403);
  });
});

describe("updateLeadStatus", () => {
  it("PATCHes a snake_case body with only the provided companions", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "lead-1", status: "lost" }));
    await updateLeadStatus({
      leadId: "lead-1",
      bearerToken: "token-1",
      status: "lost",
      rejectionReason: "Khách không nghe máy",
      reengageAt: "2026-09-01T00:00:00.000Z",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/lead-1/status");
    expect(init?.method).toBe("PATCH");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
    expect(JSON.parse(String(init?.body))).toEqual({
      status: "lost",
      rejection_reason: "Khách không nghe máy",
      reengage_at: "2026-09-01T00:00:00.000Z",
    });
  });

  it("omits the rejection companions for a plain status change", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "lead-1", status: "called" }));
    await updateLeadStatus({ leadId: "lead-1", bearerToken: "token-1", status: "called" });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      status: "called",
    });
  });
});

describe("withdrawMarketingConsent", () => {
  it("POSTs the withdraw endpoint", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 200 }));
    await withdrawMarketingConsent({ customerId: "customer-9", bearerToken: "token-1" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/customers/customer-9/withdraw-marketing-consent");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
  });
});
