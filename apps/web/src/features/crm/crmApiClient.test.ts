import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CrmApiClientError,
  fetchCrmLeadsPage,
  fetchLeadConversation,
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
  it("POSTs a normalized phone in the JSON body without exposing it in the URL", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        customer_id: "customer-9",
        leads: [{ id: "row-1", project_key: "camellia", masked_phone: "090****456" }],
      })
    );
    const profile = await searchCustomerByPhone({
      rawPhone: "090 123-4567",
      bearerToken: "token-1",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/customers/search");
    expect(String(url)).not.toContain("0901234567");
    expect(String(url)).not.toContain("phone=");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual({ phone: "0901234567" });
    expect(profile.customerId).toBe("customer-9");
    expect(profile.leads[0]).toMatchObject({ id: "row-1", project_key: "camellia" });
  });

  it("throws a typed 404 error without exposing the searched phone", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "0900123456 not found" }, 404));
    const error = await searchCustomerByPhone({
      rawPhone: "0900 123 456",
      bearerToken: "token-1",
    }).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(CrmApiClientError);
    expect((error as CrmApiClientError).status).toBe(404);
    expect((error as Error).message).not.toContain("0900123456");
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
      numericLeadId: 1,
      bearerToken: "token-1",
      status: "lost",
      rejectionReason: "Khách không nghe máy",
      reengageAt: "2026-09-01T00:00:00.000Z",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/1/status");
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
    await updateLeadStatus({
      leadId: "lead-1",
      numericLeadId: 1,
      bearerToken: "token-1",
      status: "called",
    });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      status: "called",
    });
  });

  it("keys the URL by the numeric PG lead id when provided", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "lead-1", status: "called" }));
    await updateLeadStatus({
      leadId: "lead-1",
      numericLeadId: 17,
      bearerToken: "token-1",
      status: "called",
    });
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/17/status");
  });

  it("fails safely without sending a legacy opaque id to the integer route", async () => {
    await expect(
      updateLeadStatus({
        leadId: "lead-1",
        numericLeadId: undefined,
        bearerToken: "token-1",
        status: "called",
      })
    ).rejects.toMatchObject({ name: "CrmApiClientError", status: 400 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects non-integer numeric lead ids before making a request", async () => {
    await expect(
      updateLeadStatus({
        leadId: "lead-1",
        numericLeadId: 17.5,
        bearerToken: "token-1",
        status: "called",
      })
    ).rejects.toMatchObject({ name: "CrmApiClientError", status: 400 });
    expect(fetchMock).not.toHaveBeenCalled();
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

describe("fetchLeadConversation", () => {
  it("GETs the lead conversation endpoint with the bearer token and maps the transcript", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        session_id: "session-7",
        messages: [
          { role: "user", content: "Giá bao nhiêu?", meta: null, created_at: "2026-08-25T09:00:00Z" },
          {
            role: "assistant",
            content: "Bảng giá chi tiết:",
            meta: { sources: [{ doc_id: "d1", title: "Bảng giá Camellia", kind: "price" }] },
            created_at: "2026-08-25T09:00:05Z",
          },
        ],
      })
    );
    const conversation = await fetchLeadConversation({
      leadId: "lead-42",
      numericLeadId: 42,
      bearerToken: "token-1",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/42/conversation");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
    expect(conversation.sessionId).toBe("session-7");
    expect(conversation.messages).toHaveLength(2);
    expect(conversation.messages[0]).toMatchObject({ role: "user", content: "Giá bao nhiêu?" });
  });

  it("resolves the backend 404 (no linked chat session) to an empty transcript", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Conversation not found" }, 404));
    const conversation = await fetchLeadConversation({
      leadId: "lead-42",
      numericLeadId: 42,
      bearerToken: "token-1",
    });
    expect(conversation).toEqual({ sessionId: null, messages: [] });
  });

  it("surfaces other failures as a typed error", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "forbidden" }, 403));
    const error = await fetchLeadConversation({
      leadId: "lead-42",
      numericLeadId: 42,
      bearerToken: "token-1",
    }).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(CrmApiClientError);
    expect((error as CrmApiClientError).status).toBe(403);
  });

  it("keys the URL by the numeric PG lead id when provided", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: null, messages: [] }));
    await fetchLeadConversation({
      leadId: "lead-42",
      numericLeadId: 7,
      bearerToken: "token-1",
    });
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/7/conversation");
  });

  it("fails safely without sending a legacy opaque id to the integer route", async () => {
    await expect(
      fetchLeadConversation({
        leadId: "lead-42",
        numericLeadId: undefined,
        bearerToken: "token-1",
      })
    ).rejects.toMatchObject({ name: "CrmApiClientError", status: 400 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a malformed payload without a message array", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: "s", messages: "nope" }));
    await expect(
      fetchLeadConversation({ leadId: "lead-42", numericLeadId: 42, bearerToken: "token-1" })
    ).rejects.toMatchObject({ name: "CrmApiClientError", status: 502 });
  });
});

describe("fetchCrmLeadsPage", () => {
  const wireItem = {
    id: "opaque-1",
    lead_id: 101,
    project_key: "camellia",
    display_name: "Nguyen Van A",
    masked_phone: "090****456",
    lead_status: "new",
    assigned_sales_id: 7,
    created_at: "2026-09-01T08:00:00Z",
    rejection_reason: null,
    reengage_at: null,
    consent_service: true,
    consent_marketing: true,
    marketing_consent_withdrawn_at: null,
  };

  function pageBody(items: unknown[], nextCursor: string | null, hasMore: boolean): unknown {
    return { items, next_cursor: nextCursor, has_more: hasMore, server_time: "2026-09-03T00:00:00Z" };
  }

  function pageRequest(overrides: Partial<Parameters<typeof fetchCrmLeadsPage>[0]> = {}): Parameters<typeof fetchCrmLeadsPage>[0] {
    return {
      projectKey: null,
      status: null,
      reengageFromIsoDate: null,
      reengageToIsoDate: null,
      cursor: null,
      limit: 20,
      bearerToken: "token-1",
      signal: new AbortController().signal,
      ...overrides,
    };
  }

  it("GETs the leads endpoint with bearer auth, server filters and an abortable signal", async () => {
    fetchMock.mockResolvedValue(jsonResponse(pageBody([wireItem], "cursor-2", true)));
    const page = await fetchCrmLeadsPage(
      pageRequest({
        projectKey: "camellia",
        status: "lost",
        reengageFromIsoDate: "2026-09-01",
        reengageToIsoDate: "2026-09-30",
        cursor: "cursor-1",
        limit: 50,
      })
    );
    const [url, init] = fetchMock.mock.calls[0];
    const parsed = new URL(String(url), "https://crm.test");
    expect(parsed.pathname).toBe("/api/crm/leads");
    expect(parsed.searchParams.get("project_key")).toBe("camellia");
    expect(parsed.searchParams.get("status")).toBe("lost");
    expect(parsed.searchParams.get("reengage_from")).toBe("2026-09-01");
    expect(parsed.searchParams.get("reengage_to")).toBe("2026-09-30");
    expect(parsed.searchParams.get("cursor")).toBe("cursor-1");
    expect(parsed.searchParams.get("limit")).toBe("50");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    expect(page.rows).toHaveLength(1);
    expect(page.rows[0]).toMatchObject({
      id: "opaque-1",
      leadId: 101,
      projectKey: "camellia",
      maskedPhone: "090****456",
      workflowStatus: "new",
      rejectionReason: null,
      reengageAt: null,
    });
    expect(page.nextCursor).toBe("cursor-2");
    expect(page.hasMore).toBe(true);
  });

  it("omits unset filters and the cursor from the query string", async () => {
    fetchMock.mockResolvedValue(jsonResponse(pageBody([], null, false)));
    await fetchCrmLeadsPage(pageRequest());
    const parsed = new URL(String(fetchMock.mock.calls[0][0]), "https://crm.test");
    expect(parsed.pathname).toBe("/api/crm/leads");
    expect(parsed.searchParams.get("project_key")).toBeNull();
    expect(parsed.searchParams.get("status")).toBeNull();
    expect(parsed.searchParams.get("reengage_from")).toBeNull();
    expect(parsed.searchParams.get("reengage_to")).toBeNull();
    expect(parsed.searchParams.get("cursor")).toBeNull();
    expect(parsed.searchParams.get("limit")).toBe("20");
  });

  it("rejects a malformed payload that lacks a valid items array", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: "nope", has_more: true }));
    await expect(fetchCrmLeadsPage(pageRequest())).rejects.toMatchObject({
      name: "CrmApiClientError",
      status: 502,
    });
  });

  it("propagates the backend status and detail as a typed error", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Invalid cursor" }, 422));
    const caught = await fetchCrmLeadsPage(pageRequest({ cursor: "broken" })).catch(
      (cause: unknown) => cause
    );
    expect(caught).toBeInstanceOf(CrmApiClientError);
    expect((caught as CrmApiClientError).status).toBe(422);
    expect((caught as CrmApiClientError).message).toBe("Invalid cursor");
  });
});
