// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { LeadConversationPanel } from "@/features/crm/LeadConversationPanel";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  cleanup();
  // Reset call history between tests; each case installs its own responses.
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>();

// The conversation route is integer-only: crmApiClient builds the URL from the
// numeric Postgres leads.id (mirrored onto domain Lead.leadId) and refuses the
// legacy opaque document id, so every fixture must carry a numeric id.
function renderPanel(
  leadId = "lead-77",
  bearerToken: string | null = "token-crm",
  numericLeadId: number = 9077
) {
  return render(
    <LeadConversationPanel
      leadId={leadId}
      bearerToken={bearerToken}
      numericLeadId={numericLeadId}
    />
  );
}

describe("LeadConversationPanel", () => {
  it("calls the lead conversation endpoint with the CRM shell bearer token", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: null, messages: [] }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/9077/conversation");
    expect((init?.headers as Record<string, string>).Authorization).toBe(
      "Bearer token-crm"
    );
  });

  it("renders the two-role transcript with the citation line", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        session_id: "session-7",
        messages: [
          {
            role: "user",
            content: "Chính sách thanh toán thế nào?",
            meta: null,
            created_at: "2026-08-25T09:00:00Z",
          },
          {
            role: "assistant",
            content: "| Đợt | Tỷ lệ |\n| --- | --- |\n| Đợt 1 | 30% |",
            meta: {
              sources: [{ doc_id: "d1", title: "Chính sách bán hàng Camellia", kind: "policy" }],
            },
            created_at: "2026-08-25T09:00:05Z",
          },
        ],
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel("lead-42", "token-crm", 9042);

    // Customer turn and assistant turn are both present.
    expect(await screen.findByText("Chính sách thanh toán thế nào?")).toBeTruthy();
    expect(screen.getByText(/Đợt 1/)).toBeTruthy();
    // Assistant citations collapse to one small "Nguồn: ..." line.
    expect(screen.getByText("Nguồn: Chính sách bán hàng Camellia")).toBeTruthy();
    // Speaker labels distinguish customer turns from chatbot turns.
    expect(screen.getAllByText("Khách").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Chatbot").length).toBeGreaterThan(0);
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/crm/leads/9042/conversation");
  });

  it("shows the visible message count and newest-timestamp meta above the transcript", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        session_id: "session-7",
        messages: [
          {
            role: "user",
            content: "Còn căn 2 phòng ngủ không?",
            meta: null,
            created_at: "2026-08-25T09:00:00Z",
          },
          {
            role: "assistant",
            content: "Vẫn còn ạ.",
            meta: null,
            created_at: "2026-08-25T09:05:00Z",
          },
        ],
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const meta = await screen.findByTestId("conversation-meta");
    expect(meta.textContent).toContain("2 tin nhắn");
    // Timestamp renders in the viewer's local timezone (dayjs default), so
    // assert the "mới nhất" label instead of pinning a wall-clock hour.
    expect(meta.textContent).toContain("mới nhất 25/08");
  });

  it("hides the meta line for an empty transcript", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: null, messages: [] }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    await screen.findByText("Khách chưa chat trước khi để lại số");
    expect(screen.queryByTestId("conversation-meta")).toBeNull();
  });

  it("copies the full transcript with speaker prefixes via the copy-all button", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    fetchMock.mockResolvedValue(
      jsonResponse({
        session_id: "session-7",
        messages: [
          {
            role: "user",
            content: "Giá bao nhiêu?",
            meta: null,
            created_at: "2026-08-25T09:00:00Z",
          },
          {
            role: "assistant",
            content: "Dự án từ 3,5 tỷ.",
            meta: null,
            created_at: "2026-08-25T09:00:05Z",
          },
        ],
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const copyButton = await screen.findByRole("button", {
      name: "Sao chép toàn bộ hội thoại",
    });
    // Empty transcripts disable the copy affordance; here messages exist.
    expect(copyButton.getAttribute("disabled")).toBeNull();
    fireEvent.click(copyButton);

    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(writeText).toHaveBeenCalledWith(
      "Khách: Giá bao nhiêu?\n\nChatbot: Dự án từ 3,5 tỷ."
    );
    expect(
      await screen.findByRole("button", { name: "Đã sao chép hội thoại" })
    ).toBeTruthy();
  });

  it("keeps the copy-all button disabled when no conversation loaded", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: null, messages: [] }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const copyButton = await screen.findByRole("button", {
      name: "Sao chép toàn bộ hội thoại",
    });
    expect(copyButton.getAttribute("disabled")).not.toBeNull();
  });

  it("shows the friendly empty state when the lead never chatted (404)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "Conversation not found" }, 404)
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    expect(
      await screen.findByText("Khách chưa chat trước khi để lại số")
    ).toBeTruthy();
    expect(screen.queryByText("Nguồn:")).toBeNull();
  });

  it("surfaces load failures as an error alert and retries on demand", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500))
      .mockResolvedValueOnce(
        jsonResponse({
          session_id: "session-7",
          messages: [
            {
              role: "user",
              content: "Còn căn 2 phòng ngủ không?",
              meta: null,
              created_at: "2026-08-25T09:00:00Z",
            },
          ],
        })
      );
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    expect(await screen.findByText("Không tải được hội thoại")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Thử lại/i }));
    expect(
      await screen.findByText("Còn căn 2 phòng ngủ không?")
    ).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("reloads the transcript from the Làm mới button", async () => {
    const firstLoad = deferred<Response>();
    const secondLoad = deferred<Response>();
    fetchMock
      .mockReturnValueOnce(firstLoad.promise)
      .mockReturnValueOnce(secondLoad.promise);
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    const refreshButton = screen.getByRole("button", {
      name: "Làm mới hội thoại",
    }) as HTMLButtonElement;
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    firstLoad.resolve(jsonResponse({ session_id: null, messages: [] }));
    expect(
      await screen.findByText("Khách chưa chat trước khi để lại số")
    ).toBeTruthy();
    await waitFor(() => expect(refreshButton.disabled).toBe(false));

    fireEvent.click(refreshButton);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    // The reload targets the same lead conversation URL.
    expect(String(fetchMock.mock.calls[1][0])).toBe("/api/crm/leads/9077/conversation");
    secondLoad.resolve(jsonResponse({ session_id: null, messages: [] }));
    await waitFor(() => expect(refreshButton.disabled).toBe(false));
  });

  it("never fires the request when the lead lacks a numeric id (client-side guard)", async () => {
    vi.stubGlobal("fetch", fetchMock);
    // Rendered directly (not via renderPanel) so numericLeadId is truly
    // absent — the shape a pre-mirror-era lead arrives with.
    render(
      <LeadConversationPanel leadId="lead-legacy" bearerToken="token-crm" />
    );

    // The integer-only route contract is enforced before any network call:
    // the typed 400 surfaces as the error alert, fetch stays untouched.
    expect(
      await screen.findByText("Không xác định được mã lead hợp lệ.")
    ).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("waits for the shell token instead of firing an unauthenticated request", async () => {
    vi.stubGlobal("fetch", fetchMock);
    renderPanel("lead-77", null);

    // Let microtasks flush; no request may leave without the bearer token.
    await waitFor(() => expect(screen.getByTestId("lead-conversation-panel")).toBeTruthy());
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
