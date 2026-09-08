// @vitest-environment jsdom
/**
 * Training 409 self-heal (sub-task D) over the shared ChatPage shell.
 *
 * Pinned here:
 *  1. A training-scope 409 collision drops the just-appended pair, mints a
 *     FRESH training-scope session id, warns once, and re-sends the SAME query
 *     exactly once (streamQuery called twice, second carries the new id).
 *  2. A second 409 on the retry must NOT loop: no third send, the server detail
 *     surfaces in the assistant bubble instead (stop condition).
 *  3. The heal is scoped: the customer session key is never touched and the
 *     fresh id lands on the training key only.
 * Customer-mode and transport plumbing are covered by ChatPage.trainingMode and
 * the dedicated ChatPage.* suites; this file isolates the collision recovery.
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

const firebaseQueryAuthTokenMock = vi.hoisted(() => vi.fn());
vi.mock("@/features/auth/queryAuthToken", () => ({
  firebaseQueryAuthToken: firebaseQueryAuthTokenMock,
}));

// Real-ish QueryRequestError so the self-heal's `instanceof` + `.status`/`.code`
// gates behave exactly as in production; the test constructs instances from
// this same mocked module so `instanceof` holds.
vi.mock("@/lib/api", () => {
  class QueryRequestError extends Error {
    status: number;
    code: string | null;
    body: unknown;
    constructor(status: number, message: string, code: string | null, body: unknown = null) {
      super(message);
      this.name = "QueryRequestError";
      this.status = status;
      this.code = code;
      this.body = body;
    }
  }
  return {
    QueryRequestError,
    QueryStreamError: class QueryStreamError extends Error {},
    streamQuery: vi.fn(),
    fetchGreeting: vi.fn(),
    fetchChatSessionMessages: vi.fn(),
    fetchTrainingSession: vi.fn(),
    fetchTrainingSessions: vi.fn(),
    setQueryAuthTokenProvider: vi.fn(),
  };
});

vi.mock("@/lib/projectCatalog", () => ({
  fetchProjectCatalog: vi.fn().mockResolvedValue([]),
}));

import { streamQuery, fetchGreeting, QueryRequestError } from "@/lib/api";
import { ChatPage } from "@/components/ChatPage";

type CapturedRequest = {
  query: string;
  session_id?: string;
  answer_mode?: "normal" | "training";
  context?: { project_key: string };
  signal?: AbortSignal;
};
type StreamHandlers = { onError: (err: Error) => void };

// Every streamQuery call records its request and handlers so the test can drive
// the terminal onError callback deterministically.
let calls: Array<{ req: CapturedRequest; handlers: StreamHandlers }> = [];

function sendQuestion(text: string) {
  fireEvent.change(screen.getByLabelText("Câu hỏi"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /Gửi/ }));
}

describe("ChatPage training 409 self-heal", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.sessionStorage.setItem("ragre.hello_shown", "1");
    calls = [];
    vi.mocked(fetchGreeting).mockResolvedValue({ greeting: "", suggestions: [] });
    firebaseQueryAuthTokenMock.mockReset();
    firebaseQueryAuthTokenMock.mockResolvedValue("idp_train_tok_valid");
    vi.mocked(streamQuery).mockImplementation(((
      req: CapturedRequest,
      handlers: StreamHandlers
    ) => {
      calls.push({ req, handlers });
      return Promise.resolve();
    }) as unknown as typeof streamQuery);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("retries exactly once with a fresh training id, then stops on a second 409", async () => {
    render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    sendQuestion("Chính sách giữ chỗ?");
    await waitFor(() => expect(calls).toHaveLength(1));
    const firstId = calls[0].req.session_id;
    expect(firstId).toBeTruthy();

    // First 409 collision -> self-heal: fresh id + one re-send of the same query.
    await act(async () => {
      calls[0].handlers.onError(
        new QueryRequestError(409, "xung đột phiên", "TRAINING_SESSION_CONFLICT", null)
      );
    });
    await waitFor(() => expect(calls).toHaveLength(2));

    const secondId = calls[1].req.session_id;
    expect(secondId).toBeTruthy();
    expect(secondId).not.toBe(firstId);
    expect(calls[1].req.query).toBe("Chính sách giữ chỗ?");
    expect(calls[1].req.answer_mode).toBe("training");
    // The healed id is persisted on the TRAINING key only; the customer key is
    // never written by a training send.
    expect(window.sessionStorage.getItem("ragre.session_id.training")).toBe(secondId);
    expect(window.sessionStorage.getItem("ragre.session_id")).toBeNull();
    // One-shot warning surfaced.
    await waitFor(() =>
      expect(screen.getByText(/Phiên huấn luyện không còn hợp lệ/)).toBeTruthy()
    );

    // Second 409 on the retry -> stop: no third send, server detail surfaces.
    await act(async () => {
      calls[1].handlers.onError(
        new QueryRequestError(409, "CHI_TIET_TU_SERVER", "TRAINING_SESSION_CONFLICT", null)
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(calls).toHaveLength(2);
    // Server detail surfaces (assistant bubble and/or the error toast).
    await waitFor(() =>
      expect(screen.getAllByText("CHI_TIET_TU_SERVER").length).toBeGreaterThan(0)
    );
  });

  it("treats a code-less training 409 as a conflict (older backend) and heals once", async () => {
    render(
      <AntdApp>
        <ChatPage mode="training" />
      </AntdApp>
    );

    sendQuestion("Bảng giá nội bộ?");
    await waitFor(() => expect(calls).toHaveLength(1));
    const firstId = calls[0].req.session_id;

    await act(async () => {
      calls[0].handlers.onError(new QueryRequestError(409, "mã cũ", null, null));
    });
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].req.session_id).not.toBe(firstId);
  });
});
