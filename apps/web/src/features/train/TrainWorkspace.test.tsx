// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// streamQuery is mocked so each test can drive the SSE handler callbacks; the
// real fetch/SSE plumbing is out of scope for a workspace-level unit test.
vi.mock("@/lib/api", () => ({
  QueryRequestError: class QueryRequestError extends Error {},
  streamQuery: vi.fn(),
}));

import { streamQuery } from "@/lib/api";
import type { QueryStreamHandlers } from "@/lib/api";
import type { SseRoutingPayload } from "@rag-ragre/contracts";
import { App as AntdApp } from "antd";
import { TrainWorkspace } from "./TrainWorkspace";

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
  answer_mode?: "customer" | "training";
  project_key?: string;
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

  it("sends the question with answer_mode=training and no project_key", async () => {
    renderTrainWorkspace();

    sendQuestion("Chính sách chiết khấu căn hạng B là gì?");

    await waitFor(() => expect(capturedRequest).toBeTruthy());
    // Training scope is decided server-side (story 11.3): the client must not
    // pick a namespace, only flag the mode.
    expect(capturedRequest!.answer_mode).toBe("training");
    expect(capturedRequest!.query).toBe("Chính sách chiết khấu căn hạng B là gì?");
    expect(capturedRequest!.project_key).toBeUndefined();
    expect(screen.getByText("Chính sách chiết khấu căn hạng B là gì?")).toBeTruthy();
  });

  it("renders the streamed answer with its citations and confidence badge", async () => {
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
        confidence: "HIGH",
      });
    });

    expect(await screen.findByText(/chọn căn và đặt giữ chỗ\./)).toBeTruthy();
    expect(screen.getByText("sales_kit.pdf")).toBeTruthy();
    expect(screen.getByTestId("confidence-badge").textContent).toContain("Độ tin cậy cao");
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
