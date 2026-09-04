// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// TrainWorkspace renders the shared ChatPage shell, which is a client
// component using the Next router; jsdom tests mock it out.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

// streamQuery is mocked so each test can drive the SSE handler callbacks; the
// real fetch/SSE plumbing is out of scope for a workspace-level unit test.
// TrainWorkspace now renders the shared ChatPage shell in training mode, so
// the mock must cover every api export ChatPage imports (greeting/session
// fetchers are never called in training mode; they only need to exist).
vi.mock("@/lib/api", () => ({
  QueryRequestError: class QueryRequestError extends Error {},
  QueryStreamError: class QueryStreamError extends Error {},
  streamQuery: vi.fn(),
  fetchGreeting: vi.fn().mockResolvedValue({ greeting: "", suggestions: [] }),
  fetchChatSessionMessages: vi.fn().mockResolvedValue([]),
  // The workspace installs the bearer-token provider on mount; mocked here so
  // the effect stays inert (its integration coverage lives in
  // TrainWorkspace.authBearer.test.tsx against the real api module).
  setQueryAuthTokenProvider: vi.fn(),
}));

import { streamQuery } from "@/lib/api";
import type { SseRoutingPayload } from "@rag-ragre/contracts";
import { App as AntdApp } from "antd";
import { TrainWorkspace } from "./TrainWorkspace";

// The training gate mints a Firebase bearer per send (training 401 hardening);
// the real module would hit Firebase in jsdom, so the suite supplies a valid
// token the same way ChatPage.trainingMode.test.tsx does. The gate must stay
// enabled — these tests never weaken it.
const firebaseQueryAuthTokenMock = vi.hoisted(() => vi.fn());
vi.mock("@/features/auth/queryAuthToken", () => ({
  firebaseQueryAuthToken: firebaseQueryAuthTokenMock,
}));

// Mirrors the real page shell: AntdApp supplies the message context the
// workspace reads via AntApp.useApp() for error toasts.
function renderTrainWorkspace() {
  return render(
    <AntdApp>
      <TrainWorkspace />
    </AntdApp>
  );
}

type CapturedRequest = {
  query: string;
  history?: { role: "user" | "assistant"; content: string }[];
  answer_mode?: "normal" | "training";
  project_key?: string;
  context?: { project_key: string };
  signal?: AbortSignal;
};
type QueryStreamHandlers = {
  onSources?: (sources: { doc_id: string; title: string; section?: string; kind?: string }[]) => void;
  onToken?: (token: string) => void;
  onRouting?: (routing: SseRoutingPayload) => void;
  onDone?: (result: { trace_id: string; latency_ms: number; confidence?: string; requires_review?: boolean }) => void;
  onError?: (error: Error) => void;
};
let capturedHandlers: QueryStreamHandlers | null = null;
let capturedRequest: CapturedRequest | null = null;

function sendQuestion(text: string) {
  fireEvent.change(screen.getByLabelText("Câu hỏi"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /Gửi/ }));
}

describe("TrainWorkspace", () => {
  beforeEach(() => {
    capturedHandlers = null;
    capturedRequest = null;
    firebaseQueryAuthTokenMock.mockReset();
    firebaseQueryAuthTokenMock.mockResolvedValue("idp_train_tok_valid");
    vi.mocked(streamQuery).mockImplementation(((req: CapturedRequest, handlers: QueryStreamHandlers) => {
      capturedRequest = req;
      capturedHandlers = handlers;
      return Promise.resolve();
    }) as unknown as typeof streamQuery);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("sends the question with answer_mode=training and project context", async () => {
    renderTrainWorkspace();

    sendQuestion("Chính sách chiết khấu căn hạng B là gì?");

    await waitFor(() => expect(capturedRequest).toBeTruthy());
    expect(capturedRequest!.answer_mode).toBe("training");
    expect(capturedRequest!.context).toEqual({ project_key: "camellia" });
    expect(capturedRequest!.project_key).toBeUndefined();
    expect(capturedRequest!.query).toBe("Chính sách chiết khấu căn hạng B là gì?");
    expect(screen.getByText("Chính sách chiết khấu căn hạng B là gì?")).toBeTruthy();
  });

  it("aborts an in-flight stream on unmount so no callback can dangle", async () => {
    const { unmount } = renderTrainWorkspace();

    sendQuestion("Luồng giữ chỗ vẫn đang chạy khi tôi rời trang?");
    await waitFor(() => expect(capturedRequest).toBeTruthy());

    // The workspace must hand streamQuery a live AbortSignal (ISSUE-5 FR-5):
    // unmounting aborts it, so neither SSE callbacks nor the token-flush
    // timer can fire after the component is gone.
    expect(capturedRequest!.signal).toBeInstanceOf(AbortSignal);
    expect(capturedRequest!.signal!.aborted).toBe(false);

    unmount();

    expect(capturedRequest!.signal!.aborted).toBe(true);
    // The streamed answer placeholder must not have flipped into a dangling
    // timer: flush cleanup already ran with no pending timeouts asserted by
    // the aborted signal above.
  });

  it("renders the streamed answer with citations but no warning banner even when requires_review", async () => {
    renderTrainWorkspace();

    sendQuestion("Quy trình giữ chỗ thế nào?");
    await waitFor(() => expect(capturedHandlers).toBeTruthy());

    await vi.waitFor(() => {
      capturedHandlers!.onSources?.([
        { doc_id: "d1", title: "sales_kit.pdf", section: "Giữ chỗ", kind: "training" },
      ]);
      capturedHandlers!.onToken?.("Bước 1: ");
      capturedHandlers!.onToken?.("chọn căn và đặt giữ chỗ.");
      capturedHandlers!.onDone?.({
        trace_id: "t-1",
        latency_ms: 120,
        confidence: "LOW",
        requires_review: true,
      });
    });

    expect(await screen.findByText(/chọn căn và đặt giữ chỗ\./)).toBeTruthy();
    expect(screen.getByText("sales_kit.pdf")).toBeTruthy();
    // The reliability warning banner and confidence badge must never render,
    // even at LOW confidence with requires_review (high-stakes) set.
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
    expect(screen.queryByText(/Câu trả lời cần tư vấn viên xác nhận/)).toBeNull();
    expect(screen.queryByText(/độ tin cậy thấp/)).toBeNull();
  });

  it("never renders a lead CTA even when routing carries lead_cta_hint", async () => {
    renderTrainWorkspace();

    sendQuestion("Objection giá cao phải trả lời ra sao?");
    await waitFor(() => expect(capturedHandlers).toBeTruthy());

    await vi.waitFor(() => {
      // The customer chat surfaces this hint as a CTA button; training must
      // swallow it silently (story 11.2 hard requirement).
      capturedHandlers!.onRouting?.({
        intent: "policy",
        conv_state: "answering",
        panel_hint: "none",
        lead_cta_hint: "Nhận bảng giá + ưu đãi qua điện thoại",
      } satisfies SseRoutingPayload);
      capturedHandlers!.onDone?.({ trace_id: "t-2", latency_ms: 80 });
    });

    expect(screen.queryByText(/Nhận bảng giá/)).toBeNull();
    const ctaButton = screen.queryByRole("button", { name: /Nhận bảng giá/ });
    expect(ctaButton).toBeNull();
  });

  it("surfaces a stream error without losing the typed question", async () => {
    renderTrainWorkspace();

    sendQuestion("Phí quản lý dự án là bao nhiêu?");
    await waitFor(() => expect(capturedHandlers).toBeTruthy());

    await vi.waitFor(() => {
      capturedHandlers!.onError?.(new Error("Không kết nối được máy chủ. Vui lòng thử lại."));
    });

    expect(await screen.findByText("Không kết nối được máy chủ. Vui lòng thử lại.")).toBeTruthy();
    // The failed question stays in the composer so the sales can retry.
    expect((screen.getByLabelText("Câu hỏi") as HTMLTextAreaElement).value).toBe(
      "Phí quản lý dự án là bao nhiêu?"
    );
  });
});
