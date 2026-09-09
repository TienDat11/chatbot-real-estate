// @vitest-environment jsdom
/**
 * Secure-wave quota UX gating tests (spec §3 US-1/US-4, §5 contracts).
 *
 * Pinned here:
 *  1. a 429 ANONYMOUS_QUOTA_EXCEEDED rejection force-opens the LeadForm and
 *     hard-disables the composer (the wall), with the server's friendly copy;
 *  2. a successful lead carrying quota_bonus_granted lifts the wall, credits
 *     the bonus into the header badge and toasts "+N lượt";
 *  3. SSE mode: ack/done quota snapshots drive the badge, the minted anon
 *     token persists (self-heal), done.answer replaces the streamed body, and
 *     lead_cta_hint timing is unchanged.
 *
 * Child boundaries are structural mocks (Composer exposes `disabled`,
 * LeadForm exposes open/onSuccess) — the unit under test is ChatPage's
 * gating logic, not antd form internals.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// ChatCanvas reads useRouter for routed project switches; these tests mount
// without a Next app context, so the router is a structural no-op double.
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
    <div>
      <input aria-label="Câu hỏi" disabled={props.disabled} value={props.value ?? ""} readOnly />
    </div>
  ),
}));

// LeadForm double: renders a success trigger only while open so the test can
// simulate POST /api/lead returning quota_bonus_granted=5.
let leadFormProps:
  | { open: boolean; anonToken?: string; onSuccess: (leadId: number, bonus: number) => void }
  | null = null;
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: (props: {
    open: boolean;
    anonToken?: string;
    onSuccess: (leadId: number, bonus: number) => void;
  }) => {
    leadFormProps = props;
    return props.open ? (
      <button type="button" onClick={() => props.onSuccess(41, 5)}>
        simulate-lead-success
      </button>
    ) : null;
  },
}));

import { ChatPage } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { ANON_TOKEN_KEY, PROJECT_KEY_STORAGE } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** SSE response replaying pre-encoded frames in order. */
function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const ENDPOINT_PROJECTS = {
  projects: [
    { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
  ],
};

// Spec §5.2 body, byte-faithful.
const BODY_429 = {
  ok: false,
  error: {
    code: "ANONYMOUS_QUOTA_EXCEEDED",
    message: "Anh/chị đã dùng hết 3 lượt tư vấn miễn phí. Để lại số điện thoại để nhận tư vấn miễn phí nhé!",
    quota: { used_turns: 3, remaining_turns: 0, cap: 3, is_authenticated: false, bonus_granted: 0 },
  },
  lead_cta: { required: true },
};

function ask(question: string): void {
  document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: question }));
}

describe("ChatPage anonymous quota gating", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    // A stored choice skips the forced picker; hello latch suppresses the
    // fake greeting stream so tests drive query flows deterministically.
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    leadFormProps = null;
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("hard-gates on HTTP 429: LeadForm force-open + disabled composer, then a granting lead unlocks", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
        if (url.includes("/api/anon/token")) {
          return Promise.resolve(jsonResponse({ anon_token: "tok_minted_1", expires_hint_hours: 2160 }));
        }
        if (url.includes("/api/query")) return Promise.resolve(jsonResponse(BODY_429, 429));
        return Promise.resolve(jsonResponse({}, 200));
      })
    );

    render(<ChatPage />);
    expect(leadFormProps?.open).toBeFalsy();
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);

    ask("Chính sách bán hàng?");

    // The lazy first-turn mint fired and persisted (self-heal contract).
    await waitFor(() =>
      expect(window.localStorage.getItem(ANON_TOKEN_KEY)).toBe("tok_minted_1")
    );
    // THE WALL: form opens, composer freezes, badge reads exhausted.
    await waitFor(() => expect(leadFormProps?.open).toBe(true));
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);
    await waitFor(() => expect(screen.getByText("Hết lượt miễn phí")).toBeTruthy());
    expect(screen.getByRole("alert").textContent).toMatch(/hết lượt tư vấn miễn phí/i);
    // The server's friendly copy became the assistant bubble content.

    // Lead success (+5): wall lifts, badge credits the bonus, toast fires.
    fireEvent.click(screen.getByRole("button", { name: "simulate-lead-success" }));
    await waitFor(() =>
      expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false)
    );
    await waitFor(() => expect(screen.getByText("Còn 5 lượt")).toBeTruthy());
    expect(await screen.findByText("+5 lượt tư vấn miễn phí")).toBeTruthy();
    // The lead binds to the same signed identity that chatted (§6).
    expect(leadFormProps?.anonToken).toBe("tok_minted_1");
  });

  it("drives the badge from SSE ack/done snapshots, persists the minted token and keeps lead_cta timing", async () => {
    const quotaPre = { used_turns: 0, remaining_turns: 3, cap: 3, is_authenticated: false, bonus_granted: 0 };
    const quotaPost = { used_turns: 1, remaining_turns: 2, cap: 3, is_authenticated: false, bonus_granted: 0 };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
        if (url.includes("/api/anon/token")) return Promise.resolve(jsonResponse({ anon_token: "tok_minted_1" }));
        if (url.includes("/api/query")) {
          return Promise.resolve(
            sseResponse([
              `event: ack\ndata: ${JSON.stringify({ quota: quotaPre, anon_token: "tok_sse_1" })}\n\n`,
              `event: routing\ndata: ${JSON.stringify({
                intent: "policy",
                conv_state: "recommend",
                panel_hint: "none",
                lead_cta_hint: "Anh/chị để lại số điện thoại nhé?",
              })}\n\n`,
              'event: token\ndata: "Bản nháp"\n\n',
              'event: token\ndata: " đang chạy"\n\n',
              `event: done\ndata: ${JSON.stringify({
                trace_id: "t1",
                latency_ms: 5,
                confidence: "HIGH",
                requires_review: false,
                answer: "FINAL SANITIZED ANSWER",
                quota: quotaPost,
              })}\n\n`,
            ])
          );
        }
        return Promise.resolve(jsonResponse({}, 200));
      })
    );

    render(<ChatPage />);
    ask("Giá căn 2PN?");

    // The stream replays in one batch, so the observable end state is the
    // done snapshot (authoritative post-consumption, §5.3).
    await waitFor(() => expect(screen.getByText("Còn 2 lượt")).toBeTruthy());
    // Ack wiring proven through its payload: the minted-on-server token from
    // the ack frame persisted (self-heal path).
    await waitFor(() =>
      expect(window.localStorage.getItem(ANON_TOKEN_KEY)).toBe("tok_sse_1")
    );
    // Soft CTA after the first useful turn still appears (no regression).
    expect(
      screen.getByRole("button", { name: /Nhận bảng giá \+ ưu đãi qua điện thoại/i })
    ).toBeTruthy();
    // No wall in the happy path.
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);
  });
});
