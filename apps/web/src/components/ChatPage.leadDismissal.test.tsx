// @vitest-environment jsdom
/**
 * Residual issue: LeadForm dismissal contract at the ChatPage level.
 *
 * Pinned here:
 *  1. a forced-open (429 ANONYMOUS_QUOTA_EXCEEDED) LeadForm closes when the
 *     customer dismisses it — the parent NEVER vetoes the close, even while
 *     the quota wall stands;
 *  2. after dismissal the composer STAYS blocked (wall intact) and no auto
 *     re-send/re-open happens — a retried question cannot burn turns nor
 *     resurrect the modal behind the customer's back;
 *  3. the customer can voluntarily reopen the form via the always-visible
 *     wall CTA, and dismiss it again freely.
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
    <div>
      <input aria-label="Câu hỏi" disabled={props.disabled} value={props.value ?? ""} readOnly />
    </div>
  ),
}));

// LeadForm double: exposes a dismissal trigger (mirrors X/Hủy/Đóng/ESC/mask,
// which all funnel into props.onClose) plus the success trigger.
let leadFormProps:
  | { open: boolean; onClose: () => void; onSuccess: (leadId: number, bonus: number) => void }
  | null = null;
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: (props: {
    open: boolean;
    onClose: () => void;
    onSuccess: (leadId: number, bonus: number) => void;
  }) => {
    leadFormProps = props;
    return props.open ? (
      <>
        <button type="button" onClick={props.onClose}>simulate-lead-dismiss</button>
        <button type="button" onClick={() => props.onSuccess(41, 5)}>simulate-lead-success</button>
      </>
    ) : null;
  },
}));

import { ChatPage } from "@/components/ChatPage";
import { ASK_EVENT } from "@/lib/constants";
import { PROJECT_KEY_STORAGE } from "@/features/chat/identity";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ENDPOINT_PROJECTS = {
  projects: [
    { project_key: "camellia", name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true, lat: 16.1052, lng: 108.2558 },
  ],
};

const BODY_429 = {
  ok: false,
  error: {
    code: "ANONYMOUS_QUOTA_EXCEEDED",
    message: "Anh/chị đã dùng hết lượt tư vấn miễn phí.",
    quota: { used_turns: 3, remaining_turns: 0, cap: 3, is_authenticated: false, bonus_granted: 0 },
  },
  lead_cta: { required: true },
};

function ask(question: string): void {
  document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: question }));
}

describe("ChatPage LeadForm dismissal contract", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem(PROJECT_KEY_STORAGE, "camellia");
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    leadFormProps = null;
    fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/projects")) return Promise.resolve(jsonResponse(ENDPOINT_PROJECTS));
      if (url.includes("/api/anon/token")) {
        return Promise.resolve(jsonResponse({ anon_token: "tok_minted_1" }));
      }
      if (url.includes("/api/query")) return Promise.resolve(jsonResponse(BODY_429, 429));
      return Promise.resolve(jsonResponse({}, 200));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  function queryCalls(): number {
    return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/api/query")).length;
  }

  it("closes on dismiss despite the wall, stays walled, and never auto-reopens", async () => {
    render(<ChatPage />);
    ask("Chính sách bán hàng?");
    await waitFor(() => expect(leadFormProps?.open).toBe(true));

    // Dismissal is unconditional: the parent must not veto the close even
    // though this identity is quota-exhausted.
    fireEvent.click(screen.getByRole("button", { name: "simulate-lead-dismiss" }));
    await waitFor(() => expect(leadFormProps?.open).toBe(false));

    // The exhaustion gate survives the dismissal: frozen composer + wall copy.
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toMatch(/hết lượt tư vấn miễn phí/i);

    // A retried question cannot resurrect the form (nor reach the backend):
    // the wall blocks sending, so no second /api/query ever fires.
    ask("Thử lại lần nữa");
    await waitFor(() => expect(queryCalls()).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 25));
    expect(leadFormProps?.open).toBe(false);
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);
  });

  it("lets the customer voluntarily reopen via the wall CTA and dismiss again", async () => {
    render(<ChatPage />);
    ask("Giá căn 2PN?");
    await waitFor(() => expect(leadFormProps?.open).toBe(true));

    fireEvent.click(screen.getByRole("button", { name: "simulate-lead-dismiss" }));
    await waitFor(() => expect(leadFormProps?.open).toBe(false));

    // Voluntary reopen path: the always-present keyboard-focusable wall CTA.
    const wallCta = screen.getByRole("button", { name: /Để lại số điện thoại nhận thêm lượt/i });
    fireEvent.click(wallCta);
    await waitFor(() => expect(leadFormProps?.open).toBe(true));

    // And closing again stays closed — no forced re-open loop.
    fireEvent.click(screen.getByRole("button", { name: "simulate-lead-dismiss" }));
    await waitFor(() => expect(leadFormProps?.open).toBe(false));

    // Double activation of the CTA is idempotent: one open form.
    fireEvent.click(wallCta);
    fireEvent.click(wallCta);
    await waitFor(() => expect(leadFormProps?.open).toBe(true));
  });

  it("lifts the dismissed mark only when the wall itself lifts", async () => {
    render(<ChatPage />);
    ask("Chính sách bán hàng?");
    await waitFor(() => expect(leadFormProps?.open).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "simulate-lead-dismiss" }));
    await waitFor(() => expect(leadFormProps?.open).toBe(false));

    // The wall still stands after dismissal: frozen composer + alert copy.
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toMatch(/hết lượt tư vấn miễn phí/i);

    // Voluntary reopen, then a granting lead lifts BOTH the wall and the
    // dismissal mark: the bonus pays out, the composer unfreezes, and a
    // fresh exhaustion episode may auto-open again.
    fireEvent.click(wallReopenButton());
    await waitFor(() => expect(leadFormProps?.open).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "simulate-lead-success" }));
    await waitFor(() =>
      expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false)
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

/** Locates the persistent wall CTA in whatever render pass is live. */
function wallReopenButton(): HTMLElement {
  return screen.getByRole("button", { name: /Để lại số điện thoại nhận thêm lượt/i });
}
