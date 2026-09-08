// @vitest-environment jsdom
/**
 * Training-mode isolation over the SHARED ChatPage shell (mode="training").
 *
 * Pinned here:
 *  1. Mode isolation: the training surface shows its stable intro and NONE of
 *     the customer selling surfaces (map rail, project picker button, project
 *     chip, history drawer, phone CTA, quota badge, lead form, quota wall).
 *  2. Transport: queries carry answer_mode="training" with the selected
 *     project context and never mint an anon token; the live stream is abortable
 *     on unmount. Sends are gated on the Firebase bearer provider — mocked
 *     here to a valid token so the stream fires; the null-token block path is
 *     covered by TrainWorkspace.authBearer.test.tsx.
 *  3. Shared shell navigation: shell-owned routes keep navigation in AppShell;
 *     standalone ChatPage remains a canvas without duplicate chrome.
 * Customer-mode behavior is covered by the dedicated ChatPage.*.test.tsx
 * files; sales shell navigation is covered by AppShell and route tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("@/components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-stub" />,
  DEFAULT_PROJECT: { lat: 16.1, lng: 108.25, name: "Đà Nẵng" },
}));
vi.mock("@/components/AccountControls", () => ({ AccountControls: () => <div data-testid="account-stub" /> }));
vi.mock("@/components/ChatHistoryDrawer", () => ({ ChatHistoryDrawer: () => <div data-testid="history-stub" /> }));
vi.mock("@/components/LeadForm", () => ({
  LEAD_ID_STORAGE_KEY: "ragre.lead_id",
  LeadForm: () => <div data-testid="lead-form-stub" />,
}));

// Role is injected per test via this mutable handle.
let currentUserRole: string | null = null;
vi.mock("@/lib/AuthProvider", () => ({
  useOptionalAuth: () => ({
    user: currentUserRole ? { uid: "u_test", role: currentUserRole } : null,
  }),
}));

// The training send gate resolves this provider per send; default it to a
// valid token so the transport tests exercise the fired-stream path.
const firebaseQueryAuthTokenMock = vi.hoisted(() => vi.fn());
vi.mock("@/features/auth/queryAuthToken", () => ({
  firebaseQueryAuthToken: firebaseQueryAuthTokenMock,
}));

vi.mock("@/lib/api", () => ({
  QueryRequestError: class QueryRequestError extends Error {},
  QueryStreamError: class QueryStreamError extends Error {},
  streamQuery: vi.fn(),
  fetchGreeting: vi.fn(),
  fetchChatSessionMessages: vi.fn(),
  setQueryAuthTokenProvider: vi.fn(),
}));

// The stale-mint tests drive a routed project switch to supersede an
// in-flight send; stub the catalogue so no validation redirect or retry
// timers race the assertions (empty catalogue = "cannot route", no-op).
vi.mock("@/lib/projectCatalog", () => ({
  fetchProjectCatalog: vi.fn().mockResolvedValue([]),
}));

import { streamQuery, fetchGreeting } from "@/lib/api";
import { ChatPage } from "@/components/ChatPage";

type CapturedRequest = {
  query: string;
  session_id?: string;
  answer_mode?: "normal" | "training";
  project_key?: string;
  context?: { project_key: string };
  signal?: AbortSignal;
};

/**
 * Third streamQuery argument (authOverride) — `undefined` when no override was
 * passed, `null`/object otherwise, so tests can distinguish "no override" from
 * "explicit null blocks the request".
 */
type CapturedAuthOverride = { bearerToken: string | null } | undefined;

let capturedRequest: CapturedRequest | null = null;
let capturedAuthOverride: CapturedAuthOverride = undefined;
let fetchCalls: string[] = [];

function sendQuestion(text: string) {
  fireEvent.change(screen.getByLabelText("Câu hỏi"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /Gửi/ }));
}

/** Deferred mint promise so a test can resolve a send's mint on demand. */
function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("ChatPage mode=\"training\" (shared shell)", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    currentUserRole = null;
    capturedRequest = null;
    capturedAuthOverride = undefined;
    fetchCalls = [];
    vi.mocked(fetchGreeting).mockResolvedValue({ greeting: "", suggestions: [] });
    firebaseQueryAuthTokenMock.mockReset();
    firebaseQueryAuthTokenMock.mockResolvedValue("idp_train_tok_valid");
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        fetchCalls.push(String(input));
        return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }));
      })
    );
    vi.mocked(streamQuery).mockImplementation((
      (req: CapturedRequest, _handlers: unknown, override: CapturedAuthOverride) => {
        capturedRequest = req;
        capturedAuthOverride = override;
        return Promise.resolve();
      }
    ) as unknown as typeof streamQuery);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("renders the training intro and none of the customer selling surfaces", () => {
    render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    expect(screen.getByText(/chế độ đào tạo nội bộ/)).toBeTruthy();
    // No map rail, no project picker entry, no project chip, no customer CTAs,
    // and no quota badge. Training history IS available (scoped to the selected
    // project), so the drawer renders in training mode too.
    expect(document.querySelector(".evidence-rail")).toBeNull();
    expect(screen.queryByRole("button", { name: /Đổi dự án/i })).toBeNull();
    expect(screen.queryByRole("status", { name: /Dự án đang tư vấn/i })).toBeNull();
    expect(screen.queryByTestId("history-stub")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Gọi tư vấn/i })).toBeNull();
    expect(screen.queryByRole("status", { name: /Lượt tư vấn/i })).toBeNull();
    expect(screen.queryByTestId("lead-form-stub")).toBeNull();
    // Header names the internal workspace, not a project.
    expect(screen.getByText("Đào tạo nội bộ")).toBeTruthy();
  });

  it("sends answer_mode=training with selected project context and mints no anon token", async () => {
    render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    sendQuestion("Chính sách giữ chỗ nội bộ?");

    await waitFor(() => expect(capturedRequest).toBeTruthy());
    expect(capturedRequest!.answer_mode).toBe("training");
    expect(capturedRequest!.project_key).toBeUndefined();
    expect(capturedRequest!.context).toEqual({ project_key: "camellia" });
    expect(capturedRequest!.query).toBe("Chính sách giữ chỗ nội bộ?");
    expect(capturedRequest!.signal).toBeInstanceOf(AbortSignal);
    // Single-mint: the gate-validated token rides the streamQuery call as an
    // explicit override — no second provider resolve happens before the fetch.
    expect(capturedAuthOverride).toEqual({ bearerToken: "idp_train_tok_valid" });
    // Training never touches the anon identity endpoints at all.
    expect(fetchCalls).toEqual([]);
  });

  it("pins the gate-minted token even when a later provider resolve would return null (sign-out race)", async () => {
    // Sign-out lands right after the gate mints a valid token: a second
    // resolve returns null. The old mint-then-discard design would re-resolve
    // the provider inside streamQuery and ship a bearer-less /query.
    firebaseQueryAuthTokenMock
      .mockResolvedValueOnce("idp_tok_gate_1")
      .mockResolvedValueOnce(null);

    render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    sendQuestion("Câu hỏi chạm phiên vừa hết hạn?");

    await waitFor(() => expect(capturedRequest).toBeTruthy());
    // Exactly ONE provider mint and the outgoing call carries it verbatim.
    expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(1);
    expect(capturedAuthOverride).toEqual({ bearerToken: "idp_tok_gate_1" });
  });

  it("self-heals the expired gate once auth resolves — composer re-enables without remount", async () => {
    // Auth-init race: the send lands while Firebase has no user/token yet, so
    // the gate blocks and freezes the composer behind the re-login panel.
    firebaseQueryAuthTokenMock.mockResolvedValue(null);
    const view = render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    sendQuestion("Gửi trước khi phiên sẵn sàng?");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Đăng nhập lại" })).toBeTruthy()
    );
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);

    // Auth resolves (user appears) WITHOUT a route remount: the panel
    // disappears and the composer re-enables on its own.
    currentUserRole = "sales";
    view.rerender(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    await waitFor(() =>
      expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false)
    );
    expect(screen.queryByRole("button", { name: "Đăng nhập lại" })).toBeNull();
  });

  it("a superseded send's mint completion performs no state writes (stale success cannot clear a live latch)", async () => {
    // Send A's mint is held pending; a routed project switch supersedes its
    // stream slot and send B's mint fails (latches the uid). A's mint then
    // resolves successfully — it must NOT clear B's latch: a dead stream
    // performs no state writes at all (no clear, no stream, no side effects).
    currentUserRole = "sales";
    const gateA = createDeferred<string | null>();
    firebaseQueryAuthTokenMock
      .mockImplementationOnce(() => gateA.promise)
      .mockResolvedValueOnce(null);

    const view = render(
      <AntdApp>
        <ChatPage mode="training" routeProjectKey="camellia" />
      </AntdApp>
    );

    await act(async () => {
      sendQuestion("Gửi A chờ mint?");
    });
    await waitFor(() => expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(1));

    // Supersede A via an external route move: the route-sync effect aborts
    // A's stream slot (ref nulled) and frees the composer for send B.
    await act(async () => {
      view.rerender(
        <AntdApp>
          <ChatPage mode="training" routeProjectKey="soleil" />
        </AntdApp>
      );
    });
    await waitFor(() =>
      expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false)
    );

    await act(async () => {
      sendQuestion("Gửi B mint thất bại?");
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Đăng nhập lại" })).toBeTruthy()
    );
    expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(2);

    // A's stale mint finally succeeds — B's latch must survive it untouched.
    await act(async () => {
      gateA.resolve("idp_tok_stale_success");
      await gateA.promise;
    });
    expect(screen.getByRole("button", { name: "Đăng nhập lại" })).toBeTruthy();
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(true);
    expect(vi.mocked(streamQuery)).not.toHaveBeenCalled();
  });

  it("a superseded send's late mint failure cannot re-latch a live session", async () => {
    // Send A's mint is held pending; a routed project switch supersedes it and
    // send B's mint succeeds (live session). A's mint then fails — it must NOT
    // raise the expired gate over B's healthy session.
    const gateA = createDeferred<string | null>();
    firebaseQueryAuthTokenMock
      .mockImplementationOnce(() => gateA.promise)
      .mockResolvedValueOnce("idp_tok_send_b_live");

    const view = render(
      <AntdApp>
        <ChatPage mode="training" routeProjectKey="camellia" />
      </AntdApp>
    );

    await act(async () => {
      sendQuestion("Gửi A chờ mint?");
    });
    await waitFor(() => expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      view.rerender(
        <AntdApp>
          <ChatPage mode="training" routeProjectKey="soleil" />
        </AntdApp>
      );
    });
    await waitFor(() =>
      expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false)
    );

    await act(async () => {
      sendQuestion("Gửi B thành công?");
    });
    await waitFor(() => expect(capturedRequest).toBeTruthy());
    expect(capturedAuthOverride).toEqual({ bearerToken: "idp_tok_send_b_live" });
    expect(screen.queryByRole("button", { name: "Đăng nhập lại" })).toBeNull();

    // A's stale mint finally fails — the live session must stay untouched.
    await act(async () => {
      gateA.resolve(null);
      await gateA.promise;
    });
    expect(screen.queryByRole("button", { name: "Đăng nhập lại" })).toBeNull();
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).disabled).toBe(false);
    expect(vi.mocked(streamQuery)).toHaveBeenCalledTimes(1);
    expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(2);
  });

  it("aborts the in-flight stream on unmount", async () => {
    const { unmount } = render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    sendQuestion("Luồng vẫn đang chạy khi tôi rời trang?");
    await waitFor(() => expect(capturedRequest).toBeTruthy());
    expect(capturedRequest!.signal!.aborted).toBe(false);

    unmount();

    expect(capturedRequest!.signal!.aborted).toBe(true);
  });

  it("keeps standalone training canvas free of duplicate shell navigation", () => {
    currentUserRole = "sales";
    render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    expect(screen.queryByRole("navigation", { name: "Điều hướng chính" })).toBeNull();
    expect(screen.getByText(/chế độ đào tạo nội bộ/)).toBeTruthy();
  });

  it("customer mode keeps the project chip and picker entry (regression guard)", () => {
    render(
      <AntdApp>
        <ChatPage />
      </AntdApp>
    );

    expect(screen.getByRole("button", { name: /Đổi dự án/i })).toBeTruthy();
    expect(screen.getByRole("status", { name: /Chưa chọn dự án|Dự án đang tư vấn/i })).toBeTruthy();
  });
});
