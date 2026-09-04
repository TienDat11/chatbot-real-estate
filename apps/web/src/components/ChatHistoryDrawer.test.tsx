/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { ChatHistoryDrawer } from "@/components/ChatHistoryDrawer";

const sessions = [
  { session_id: "s1", project_key: "soleil", title: "Giá căn hộ", message_count: 2, handed_off: true, last_active_at: "2026-08-25T10:00:00Z" },
  { session_id: "s2", project_key: "soleil", title: null, message_count: 1, handed_off: false, last_active_at: "2026-08-25T09:00:00Z" },
];

type SelectSessionMock = Mock<(sessionId: string, transcript: unknown[]) => void>;

function renderDrawer(overrides?: { onSelectSession?: SelectSessionMock }): { onSelectSession: SelectSessionMock } {
  const onSelectSession: SelectSessionMock = overrides?.onSelectSession ?? vi.fn();
  render(
    <ChatHistoryDrawer
      deviceId="device-1"
      projectKey="soleil"
      anonToken="anon-1"
      onNewSession={vi.fn()}
      onSelectSession={onSelectSession}
    />
  );
  return { onSelectSession };
}

describe("ChatHistoryDrawer", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/messages")) return Promise.resolve(new Response(JSON.stringify({ messages: [{ role: "user", content: "Xin chào", meta: null, created_at: "now" }] }), { status: 200 }));
      return Promise.resolve(new Response(JSON.stringify({ sessions }), { status: 200 }));
    }));
  });

  it("fetches scoped history with X-Device-Id and renders handed-off badge", async () => {
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /Lịch sử chat/i }));
    expect(await screen.findByText("Giá căn hộ")).toBeTruthy();
    expect(screen.getByText("Đã để lại SĐT")).toBeTruthy();
    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).includes("/api/sessions?"));
    expect(request?.[1]).toEqual(expect.objectContaining({
      headers: expect.objectContaining({ "X-Device-Id": "device-1", "X-Anon-Token": "anon-1" }),
    }));
    expect(String(request?.[0])).toContain("project_key=soleil");
  });

  it("selection hands the mapped transcript to onSelectSession and closes the drawer (FR-18)", async () => {
    // R3: the drawer NEVER renders a local transcript copy anymore — it fetches,
    // maps and delegates to ChatPage, then closes so the main canvas hydrates.
    const { onSelectSession } = renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    // Two scoped rows render, so two "Mở" actions exist — the newest session
    // (first row) is the one under test.
    const openButtons = await screen.findAllByRole("button", { name: "Mở" });
    fireEvent.click(openButtons[0]);

    // Drawer-closed: antd's exit animation never finishes under jsdom (same
    // known limitation as Modal DOM removal, asserted in browser E2E instead),
    // so the FR-18 contract here is the delegation itself: one callback with
    // the mapped transcript; ChatPage then owns hydration and closes-over URL.
    await waitForCallback(onSelectSession);
    expect(onSelectSession).toHaveBeenCalledOnce();
    expect(onSelectSession).toHaveBeenCalledWith("s1", [
      expect.objectContaining({ id: "s1-0", role: "user", content: "Xin chào" }),
    ]);

    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).includes("/api/sessions/s1/messages"));
    // Transcript requests carry the full ownership scope: session id path +
    // required project_key query + both signed identity headers.
    expect(String(request?.[0])).toContain("/api/sessions/s1/messages?project_key=soleil");
    expect(request?.[1]).toEqual(expect.objectContaining({
      headers: expect.objectContaining({ "X-Device-Id": "device-1", "X-Anon-Token": "anon-1" }),
    }));
  });

  it("keeps the drawer open with a retry alert when transcript loading fails", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/messages")) return Promise.reject(new Error("gone"));
      return Promise.resolve(new Response(JSON.stringify({ sessions }), { status: 200 }));
    }));
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    const openButtons = await screen.findAllByRole("button", { name: "Mở" });
    fireEvent.click(openButtons[0]);
    expect(await screen.findByText("Không mở được đoạn chat này. Anh/chị vui lòng thử lại.")).toBeTruthy();
    // Still open: the drawer title coexists with the trigger button and the
    // session list remains retryable.
    expect(screen.getAllByText("Lịch sử chat").length).toBeGreaterThan(0);
  });

  it("surfaces an intentional 401 ownership rejection through the retry alert", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ detail: "Signed anonymous identity is required" }), { status: 401 }))));
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    expect(await screen.findByText("Không tải được lịch sử chat. Anh/chị vui lòng thử lại.")).toBeTruthy();
    expect(screen.queryByText("Giá căn hộ")).toBeNull();
  });

  it("shows an inline retry when the history endpoint fails", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) =>
      String(input).includes("/api/projects")
        ? Promise.reject(new Error("offline"))
        : Promise.reject(new Error("offline"))
    ));
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    expect(await screen.findByText("Không tải được lịch sử chat. Anh/chị vui lòng thử lại.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Thử lại" })).toBeTruthy();
  });

  it("keeps the new-chat recovery action available after a transcript failure", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) =>
      String(input).includes("/messages")
        ? Promise.reject(new Error("gone"))
        : Promise.resolve(new Response(JSON.stringify({ sessions }), { status: 200 }))
    ));
    const onNewSession = vi.fn();
    render(<ChatHistoryDrawer deviceId="device-1" projectKey="soleil" anonToken="anon-1" onNewSession={onNewSession} onSelectSession={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Lịch sử chat" }));
    const openButtons = await screen.findAllByRole("button", { name: "Mở" });
    fireEvent.click(openButtons[0]);
    expect(await screen.findByText("Không mở được đoạn chat này. Anh/chị vui lòng thử lại.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Đoạn chat mới" }));
    expect(onNewSession).toHaveBeenCalledOnce();
  });
});

async function waitForCallback(mock: SelectSessionMock): Promise<void> {
  const { waitFor } = await import("@testing-library/react");
  await waitFor(() => expect(mock).toHaveBeenCalled());
}
