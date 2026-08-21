import { afterEach, describe, expect, it, vi } from "vitest";
import { LeadSubmitError, readErrorDetail, submitLead } from "@/lib/api";

// Story 5.7 submit flow: POST /api/lead is classified into success (201),
// duplicate (409), validation (other 4xx) and network (fetch failure / 5xx).
// fetch is stubbed globally so no HTTP server is needed.
const PAYLOAD = { phone: "0905123456", consent: true };

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("submitLead", () => {
  it("resolves with the lead id and call-back window on 201", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ lead_id: 7, will_call_within_minutes: 5 }, 201)));
    const result = await submitLead(PAYLOAD);
    expect(result).toEqual({ lead_id: 7, will_call_within_minutes: 5 });
  });

  it("posts a JSON body to /api/lead", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ lead_id: 1, will_call_within_minutes: 5 }, 201));
    vi.stubGlobal("fetch", fetchMock);
    await submitLead(PAYLOAD);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/lead",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(PAYLOAD),
      })
    );
  });

  it("throws LeadSubmitError kind=duplicate with status 409 on 409", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "duplicate" }, 409)));
    const promise = submitLead(PAYLOAD);
    await expect(promise).rejects.toBeInstanceOf(LeadSubmitError);
    await expect(promise).rejects.toMatchObject({ kind: "duplicate", status: 409 });
  });

  it("throws LeadSubmitError kind=validation with backend detail on 422", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "Số điện thoại chưa đúng định dạng Việt Nam" }, 422)));
    const promise = submitLead(PAYLOAD);
    await expect(promise).rejects.toBeInstanceOf(LeadSubmitError);
    await expect(promise).rejects.toMatchObject({
      kind: "validation",
      status: 422,
      message: "Số điện thoại chưa đúng định dạng Việt Nam",
    });
  });

  it("throws kind=validation with a fallback message when the 4xx body has no string detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: { code: "X" } }, 400)));
    const promise = submitLead(PAYLOAD);
    await expect(promise).rejects.toMatchObject({
      kind: "validation",
      message: "Thông tin chưa hợp lệ. Vui lòng kiểm tra lại.",
    });
  });

  it("throws kind=network on fetch rejection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const promise = submitLead(PAYLOAD);
    await expect(promise).rejects.toBeInstanceOf(LeadSubmitError);
    await expect(promise).rejects.toMatchObject({ kind: "network", status: null });
  });

  it("throws kind=network on 5xx", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "boom" }, 500)));
    const promise = submitLead(PAYLOAD);
    await expect(promise).rejects.toMatchObject({ kind: "network", status: 500 });
  });
});

describe("readErrorDetail", () => {
  it("returns the string detail when present", async () => {
    const detail = await readErrorDetail(jsonResponse({ detail: "Consent is required" }, 400));
    expect(detail).toBe("Consent is required");
  });

  it("returns null when the detail is not a string", async () => {
    expect(await readErrorDetail(jsonResponse({ detail: { code: "X" } }, 400))).toBeNull();
  });

  it("returns null when the body is not valid JSON", async () => {
    const response = new Response("not json", { status: 422 });
    expect(await readErrorDetail(response)).toBeNull();
  });
});
