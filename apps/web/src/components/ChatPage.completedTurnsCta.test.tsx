// @vitest-environment jsdom
/**
 * Regression coverage for the live finding: after three completed customer
 * streams, the backend's durable lead_cta_hint is delivered on done, not
 * necessarily routing. The CTA remains voluntary and must not submit a lead
 * or duplicate the query.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));
vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/MessageList", () => ({ MessageList: () => <div data-testid="messages-stub" /> }));
vi.mock("@/components/Composer", () => ({
  Composer: (props: { disabled?: boolean; value?: string }) => (
    <input aria-label="Câu hỏi" disabled={props.disabled} value={props.value ?? ""} readOnly />
  ),
}));

let leadFormProps: { open: boolean; onClose: () => void } | null = null;
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: (props: { open: boolean; onClose: () => void }) => {
    leadFormProps = props;
    return props.open ? (
      <button type="button" onClick={props.onClose}>dismiss-lead-form</button>
    ) : null;
  },
}));

import { ChatPage } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const frame of frames) controller.enqueue(encoder.encode(frame));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

function ask(question: string): void {
  document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: question }));
}

describe("ChatPage completed customer turns CTA contract", () => {
  let queryCount = 0;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    leadFormProps = null;
    queryCount = 0;
    fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/projects")) return Promise.resolve(jsonResponse({ projects: [] }));
      if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({ anon_token: "tok-1" }));
      if (url.includes("/api/query")) {
        queryCount += 1;
        const completedTurns = queryCount;
        const hint = completedTurns >= 3 ? "Anh/chị để lại số điện thoại nhé" : null;
        return Promise.resolve(sseResponse([
          `event: routing\ndata: ${JSON.stringify({ intent: "rag", conv_state: "qualify", panel_hint: "none", lead_cta_hint: null })}\n\n`,
          `event: done\ndata: ${JSON.stringify({ answer: `answer-${completedTurns}`, confidence: "HIGH", requires_review: false, lead_cta_hint: hint, quota: { used_turns: completedTurns, remaining_turns: 3 - completedTurns, cap: 3, is_authenticated: false, bonus_granted: 0 } })}\n\n`,
        ]));
      }
      return Promise.resolve(jsonResponse({}, 200));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("renders the durable done-frame CTA after three completed streams and keeps it voluntary", async () => {
    render(<ChatPage />);
    ask("Giá căn 2PN?");
    await waitFor(() => expect(screen.getByText("Còn 2 lượt")).toBeTruthy());
    expect(queryCount).toBe(1);
    ask("Chính sách thanh toán?");
    await waitFor(() => expect(screen.getByText("Còn 1 lượt")).toBeTruthy());
    expect(queryCount).toBe(2);
    ask("Tiến độ bàn giao?");
    await waitFor(() => expect(screen.getByText("Hết lượt miễn phí")).toBeTruthy());
    expect(queryCount).toBe(3);

    const cta = await screen.findByRole("button", { name: /Nhận bảng giá \+ ưu đãi qua điện thoại/i });
    expect(cta).toBeTruthy();
    fireEvent.click(cta);
    await waitFor(() => expect(leadFormProps?.open).toBe(true));
    expect(queryCount).toBe(3);

    fireEvent.click(screen.getByRole("button", { name: "dismiss-lead-form" }));
    await waitFor(() => expect(leadFormProps?.open).toBe(false));
    expect(queryCount).toBe(3);
  });

  it("does not show a CTA for a completed LOW/review-required answer without a hint", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/projects")) return Promise.resolve(jsonResponse({ projects: [] }));
      if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({ anon_token: "tok-1" }));
      if (url.includes("/api/query")) return Promise.resolve(sseResponse([
        `event: routing\ndata: ${JSON.stringify({ intent: "rag", conv_state: "qualify", panel_hint: "none", lead_cta_hint: null })}\n\n`,
        `event: done\ndata: ${JSON.stringify({ answer: "not grounded", confidence: "LOW", requires_review: true, lead_cta_hint: null, quota: { used_turns: 1, remaining_turns: 2, cap: 3, is_authenticated: false, bonus_granted: 0 } })}\n\n`,
      ]));
      return Promise.resolve(jsonResponse({}, 200));
    });
    render(<ChatPage />);
    ask("Thông tin chưa có trong hồ sơ?");
    await waitFor(() => expect(screen.getByText("Còn 2 lượt")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Nhận bảng giá \+ ưu đãi qua điện thoại/i })).toBeNull();
  });
});
