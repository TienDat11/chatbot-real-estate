// @vitest-environment jsdom
/**
 * TrainWorkspace bearer wiring (training-auth gap): while the training
 * workspace is mounted the Firebase ID-token provider is installed, so a
 * signed-in sales user's answer_mode="training" query ships
 * `Authorization: Bearer <token>` on /query (backend story 11.3 rejects
 * training answers without verified sales auth); a signed-out provider (null)
 * blocks the send entirely and surfaces a re-login state — no unauthenticated
 * /query request may ever leave the training workspace.
 *
 * The provider module itself is mocked — the unit under test is the
 * TrainWorkspace registration + api.ts header merge, not firebase/auth. No
 * credentials appear in this file; tokens are opaque synthetic strings.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp } from "antd";

// TrainWorkspace renders the shared ChatPage shell, which is a client
// component using the Next router; jsdom tests mock it out.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

const firebaseQueryAuthTokenMock = vi.hoisted(() => vi.fn());

vi.mock("@/features/auth/queryAuthToken", () => ({
  firebaseQueryAuthToken: firebaseQueryAuthTokenMock,
}));

// Capture the auth-state subscription so a test can emit a signed-in user
// WITHOUT remounting — exactly how the real AuthProvider learns about login.
const authState = vi.hoisted(() => ({
  listeners: [] as ((user: unknown) => void)[],
  emit(user: unknown): void {
    for (const listener of [...authState.listeners]) listener(user);
  },
}));

vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  onAuthChange: (callback: (user: unknown) => void) => {
    authState.listeners.push(callback);
    callback(null);
    return () => {
      authState.listeners = authState.listeners.filter((l) => l !== callback);
    };
  },
  signInWithEmail: vi.fn(),
  signUpWithEmail: vi.fn(),
  signOutUser: vi.fn(),
  getFreshIdToken: vi.fn(async () => {
    throw new Error("mocked out; use the firebaseQueryAuthToken provider seam");
  }),
}));

// REAL @/lib/api on purpose: these tests prove the end-to-end seam
// (component install -> resolveQueryAuthHeaders -> outgoing fetch headers).
import { AuthProvider } from "@/lib/AuthProvider";
import { TrainWorkspace } from "./TrainWorkspace";
import { setQueryAuthTokenProvider, streamQuery } from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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

/** Fetch double recording every /query call; stubs anon token minting. */
function stubFetchWithQueryCapture(queryFrames: string[]): { queryCalls: { init: RequestInit }[] } {
  const queryCalls: { init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/anon/token")) {
        return Promise.resolve(jsonResponse({ anon_token: "tok_minted_1" }));
      }
      if (url.includes("/api/query")) {
        queryCalls.push({ init: init ?? {} });
        return Promise.resolve(sseResponse(queryFrames));
      }
      return Promise.resolve(jsonResponse({}, 200));
    })
  );
  return { queryCalls };
}

function renderTrainWorkspace(withAuth = false) {
  // Mirrors apps/web/src/app/train/page.tsx: the workspace mounts inside an
  // AntdApp that supplies the message context for error toasts. The auth
  // variant adds the real AuthProvider (over the mocked firebase service) so
  // the auth-state latch is exercised end to end.
  return render(
    <AntdApp>
      {withAuth ? (
        <AuthProvider>
          <TrainWorkspace />
        </AuthProvider>
      ) : (
        <TrainWorkspace />
      )}
    </AntdApp>
  );
}

function ask(question: string): void {
  fireEvent.change(screen.getByLabelText("Câu hỏi"), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /Gửi/ }));
}

describe("TrainWorkspace /query bearer wiring", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    firebaseQueryAuthTokenMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    setQueryAuthTokenProvider(null);
    vi.unstubAllGlobals();
  });

  const TRAINING_FRAMES = [
    `event: ack\ndata: ${JSON.stringify({ ack: true })}\n\n`,
    // A customer-chat routing payload carries a sales CTA hint; training must
    // swallow it (story 11.2 hard requirement) even while authenticated.
    `event: routing\ndata: ${JSON.stringify({
      intent: "policy",
      conv_state: "answering",
      panel_hint: "none",
      lead_cta_hint: "Nhận bảng giá + ưu đãi qua điện thoại",
    })}\n\n`,
    `event: done\ndata: ${JSON.stringify({
      trace_id: "tr1",
      latency_ms: 5,
      answer: "OK",
      confidence: "HIGH",
    })}\n\n`,
  ];

  it("a signed-in sales training query carries Bearer <id token>, stays training mode and shows no lead CTA", async () => {
    firebaseQueryAuthTokenMock.mockResolvedValue("idp_sales_tok_train_1");
    const { queryCalls } = stubFetchWithQueryCapture(TRAINING_FRAMES);

    renderTrainWorkspace();
    ask("Chính sách giữ chỗ nội bộ?");

    // The registered provider resolved a fresh token per send...
    await waitFor(() => expect(firebaseQueryAuthTokenMock).toHaveBeenCalled());
    // ...and the outgoing /query carried it as a Bearer credential...
    await waitFor(() => expect(queryCalls.length).toBeGreaterThan(0));
    const headers = (queryCalls[0].init.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer idp_sales_tok_train_1");
    // ...with the training contract intact (answer_mode=training, scoped by
    // session id, no direct namespace request).
    const body = JSON.parse(String(queryCalls[0].init.body));
    expect(body.answer_mode).toBe("training");
    expect(body.project_key).toBeUndefined();
    expect(String(body.session_id)).not.toBe("");
    // Customer lead CTA is never surfaced in the training workspace.
    expect(screen.queryByRole("button", { name: /Nhận bảng giá/ })).toBeNull();
    expect(screen.queryByText(/Nhận bảng giá/)).toBeNull();
  });

  it("single mint: a sign-out right after the gate token cannot yield a second resolve or a bearer-less /query", async () => {
    // Regression (QC blocker): the old design resolved the provider at the
    // gate AND again inside streamQuery, so a sign-out between the two awaits
    // stripped the Authorization header. Single-mint pins the gate token to
    // the actual request — the provider is consulted exactly once and the
    // queued null second resolve is never consumed.
    firebaseQueryAuthTokenMock
      .mockResolvedValueOnce("idp_tok_gate_1")
      .mockResolvedValueOnce(null);
    const { queryCalls } = stubFetchWithQueryCapture(TRAINING_FRAMES);

    renderTrainWorkspace();
    ask("Câu hỏi trong phiên đang đổi?");

    await waitFor(() => expect(queryCalls.length).toBe(1));
    // One mint, one credential: the wire token IS the gate token, never empty.
    expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(1);
    expect(((queryCalls[0].init.headers ?? {}) as Record<string, string>).Authorization).toBe(
      "Bearer idp_tok_gate_1"
    );
  });

  it("a signed-out provider never fires /query and surfaces the re-login state", async () => {
    firebaseQueryAuthTokenMock.mockResolvedValue(null);
    const { queryCalls } = stubFetchWithQueryCapture(TRAINING_FRAMES);

    renderTrainWorkspace();
    ask("Quy trình đào tạo cơ bản?");

    // The training send resolved the bearer provider first, then blocked:
    // the optimistic bubbles are rolled back and NO unauthenticated /query
    // request is emitted (the backend would only answer 401). The re-login
    // button lives inside the alert panel, so its presence pins both.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Đăng nhập lại" })).toBeTruthy()
    );
    expect(queryCalls).toHaveLength(0);
    // The optimistic user bubble is gone but the draft stays in the composer,
    // so a re-login does not force retyping.
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).value).toBe(
      "Quy trình đào tạo cơ bản?"
    );
    expect(screen.getByLabelText("Câu hỏi")).toHaveProperty("disabled", true);
  });

  it("re-auth without remount: the streaming lock releases and the next send fires exactly one Bearer /query", async () => {
    // Regression (stuck composer): handleSend raises the streaming flag
    // BEFORE awaiting the bearer provider. When that first send resolves
    // null, the terminal branch must release the flag — otherwise the
    // composer's canSend stays false forever even after the auth latch
    // self-heals on re-login, and every later send is silently swallowed.
    firebaseQueryAuthTokenMock
      .mockResolvedValueOnce(null) // first send: expired / absent session
      .mockResolvedValueOnce("idp_tok_reauth_1"); // second send: signed back in
    const { queryCalls } = stubFetchWithQueryCapture(TRAINING_FRAMES);

    renderTrainWorkspace(true);
    ask("Quy trình đào tạo cơ bản?");

    // First send is blocked: no unauthenticated /query, re-login state shown,
    // draft preserved for the retry after login.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Đăng nhập lại" })).toBeTruthy()
    );
    expect(queryCalls).toHaveLength(0);
    expect((screen.getByLabelText("Câu hỏi") as HTMLInputElement).value).toBe(
      "Quy trình đào tạo cơ bản?"
    );

    // The user signs back in. onAuthChange fires in place — the component is
    // NOT remounted, matching the real re-login flow.
    act(() => {
      authState.emit({
        uid: "test-uid-reauth-1",
        email: null,
        displayName: null,
        photoURL: null,
        role: null,
      });
    });

    // The self-healing latch ("" + a materialized uid) lifts the wall, and
    // the released streaming flag lets the send button go live again.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Gửi/ })).toHaveProperty("disabled", false)
    );
    expect(screen.queryByRole("button", { name: "Đăng nhập lại" })).toBeNull();

    ask("Quy trình đào tạo cơ bản?");

    // Exactly one fresh resolve and exactly one /query, carrying the new
    // bearer credential in training mode — no double-send, no stale mint.
    await waitFor(() => expect(queryCalls).toHaveLength(1));
    expect(firebaseQueryAuthTokenMock).toHaveBeenCalledTimes(2);
    expect(((queryCalls[0].init.headers ?? {}) as Record<string, string>).Authorization).toBe(
      "Bearer idp_tok_reauth_1"
    );
    expect(JSON.parse(String(queryCalls[0].init.body)).answer_mode).toBe("training");
  });

  it("unmount clears the global provider so later queries stay anonymous", async () => {
    firebaseQueryAuthTokenMock.mockResolvedValue("idp_sales_tok_train_2");
    const { queryCalls } = stubFetchWithQueryCapture(TRAINING_FRAMES);

    const { unmount } = renderTrainWorkspace();
    ask("Kiểm tra dọn dẹp seam?");
    await waitFor(() => expect(queryCalls.length).toBeGreaterThan(0));
    expect(((queryCalls[0].init.headers ?? {}) as Record<string, string>).Authorization).toBe(
      "Bearer idp_sales_tok_train_2"
    );

    // Unmounting resets the module-level seam (same contract as ChatPage), so
    // a subsequent streamQuery through the REAL api module ships no credential
    // even though the mocked provider would resolve a token.
    unmount();
    const afterUnmount: { init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/api/query")) {
          afterUnmount.push({ init: init ?? {} });
          return Promise.resolve(sseResponse(TRAINING_FRAMES));
        }
        return Promise.resolve(jsonResponse({}, 200));
      })
    );
    await streamQuery(
      { query: "sau khi rời trang", session_id: "s-clean", answer_mode: "training" },
      { onAck: () => {}, onDone: () => {} }
    );
    expect(afterUnmount.length).toBe(1);
    expect(((afterUnmount[0].init.headers ?? {}) as Record<string, string>).Authorization).toBeUndefined();
  });
});
