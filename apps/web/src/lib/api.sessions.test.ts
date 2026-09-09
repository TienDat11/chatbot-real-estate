import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchChatSessionMessages, fetchChatSessions } from "@/lib/api";

// History/session request scope (secure wave): GET /api/sessions and
// GET /api/sessions/{id}/messages both require the X-Device-Id + X-Anon-Token
// identity headers, project_key (query), and the session id path segment.
// A missing/invalid identity is an intentional backend ownership rejection
// (401/404) that must propagate as a thrown Error, never be retried silently.
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchChatSessions", () => {
  it("sends device_id/project_key query params with both identity headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ sessions: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchChatSessions("device-1", "soleil", "anon-1");
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/sessions?device_id=${encodeURIComponent("device-1")}&project_key=${encodeURIComponent("soleil")}`,
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Device-Id": "device-1", "X-Anon-Token": "anon-1" }),
      })
    );
  });

  it("omits X-Anon-Token when no token is stored, keeping the rejection honest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "Signed anonymous identity is required" }, 401));
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchChatSessions("device-1", "soleil", null)).rejects.toThrow("sessions failed: 401");
    const headers = (fetchMock.mock.calls[0]?.[1] as RequestInit).headers as Record<string, string>;
    expect(headers["X-Device-Id"]).toBe("device-1");
    expect(headers).not.toHaveProperty("X-Anon-Token");
  });

  it("accepts both the envelope and bare-array response shapes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([{ session_id: "s1" }])));
    await expect(fetchChatSessions("d", "p", "t")).resolves.toEqual([{ session_id: "s1" }]);
  });
});

describe("fetchChatSessionMessages", () => {
  it("appends the required project_key query to the session path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ messages: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchChatSessionMessages("device-1", "sess 9", "soleil", "anon-1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/sessions/${encodeURIComponent("sess 9")}/messages?project_key=soleil`);
    expect(init.headers).toEqual(expect.objectContaining({
      Accept: "application/json",
      "X-Device-Id": "device-1",
      "X-Anon-Token": "anon-1",
    }));
  });

  it("propagates an ownership 404 instead of swallowing it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "Session not found" }, 404)));
    await expect(fetchChatSessionMessages("device-1", "other-user-session", "camellia", "anon-1"))
      .rejects.toThrow("session messages failed: 404");
  });
});
