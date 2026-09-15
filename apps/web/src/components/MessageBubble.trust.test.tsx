// @vitest-environment jsdom
/**
 * Trust-safety UI contract (W1-05, re-scoped to the surface seam): the
 * backend's confidence/review signals are internal diagnostics. They render
 * ONLY when the parent opts in via `showTrustDiagnostics` (training workspace);
 * the customer and sales storefronts omit the flag and must never see them.
 *  A. default (customer/sales): no banner, no badge, no trace/latency footer,
 *     while the answer content itself stays visible;
 *  B. internal (showTrustDiagnostics=true): banner on every lifecycle state
 *     the flag belongs to (finished, streaming, errored), 3-tier badge on the
 *     done frame only, trace footer present;
 *  C. the training end-to-end path is guarded by TrainWorkspace.test.tsx,
 *     which must stay byte-identical and passing.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MessageBubble, type ChatMessage } from "@/components/MessageBubble";

const BANNER_HEADING = "Câu trả lời cần tư vấn viên xác nhận";

function baseMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "Căn 2PN diện tích 70 m2, tham khảo sổ đỏ.",
    ...overrides,
  };
}

function renderBubble(message: ChatMessage, showTrustDiagnostics?: boolean) {
  return render(
    <AntApp>
      <MessageBubble message={message} showTrustDiagnostics={showTrustDiagnostics} />
    </AntApp>,
  );
}

afterEach(() => {
  cleanup();
});

describe("MessageBubble customer/sales surface (diagnostics hidden by default)", () => {
  it("hides every diagnostic on a flagged LOW-confidence finished answer", () => {
    renderBubble(baseMessage({ requires_review: true, confidence: "LOW", traceId: "t1", latencyMs: 120 }));
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
    expect(screen.queryByText(/trace_id/)).toBeNull();
    expect(screen.queryByText(/phản hồi trong/)).toBeNull();
    // The answer itself must remain visible: hiding diagnostics never hides content.
    expect(screen.getByText(/Căn 2PN diện tích 70 m2/)).toBeTruthy();
  });

  it("hides the badge for MEDIUM and HIGH too", () => {
    renderBubble(baseMessage({ confidence: "MEDIUM" }));
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
    cleanup();
    renderBubble(baseMessage({ confidence: "HIGH" }));
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
  });

  it("hides diagnostics while streaming and alongside an error", () => {
    renderBubble(baseMessage({ content: "Đang soạn", streaming: true, requires_review: true, confidence: "LOW" }));
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    cleanup();
    renderBubble(baseMessage({ content: "Máy chủ trả lỗi 500.", error: true, requires_review: true }));
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    // Error copy is content, not a diagnostic: still visible.
    expect(screen.getByText("Máy chủ trả lỗi 500.")).toBeTruthy();
  });

  it("explicit false behaves like the default", () => {
    renderBubble(baseMessage({ requires_review: true, confidence: "LOW", traceId: "t9" }), false);
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
    expect(screen.queryByText(/trace_id/)).toBeNull();
  });
});

describe("MessageBubble internal surface (showTrustDiagnostics=true)", () => {
  it("renders the banner on a finished flagged answer", () => {
    renderBubble(baseMessage({ requires_review: true, confidence: "LOW", traceId: "t1" }), true);
    expect(screen.getByText(BANNER_HEADING)).toBeTruthy();
  });

  it("renders the banner even while the answer is still streaming", () => {
    renderBubble(baseMessage({ content: "Đang soạn", streaming: true, requires_review: true }), true);
    expect(screen.getByText(BANNER_HEADING)).toBeTruthy();
  });

  it("renders the banner alongside a plain error message", () => {
    renderBubble(baseMessage({ content: "Máy chủ trả lỗi 500.", error: true, requires_review: true }), true);
    expect(screen.getByText(BANNER_HEADING)).toBeTruthy();
    expect(screen.getByText("Máy chủ trả lỗi 500.")).toBeTruthy();
  });

  it("renders no banner when the flag is false or absent", () => {
    renderBubble(baseMessage({ requires_review: false, confidence: "HIGH" }), true);
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    cleanup();
    renderBubble(baseMessage({ confidence: "HIGH" }), true);
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
  });

  it("shows the red LOW badge as an explicit warning", () => {
    renderBubble(baseMessage({ confidence: "LOW" }), true);
    expect(screen.getByTestId("confidence-badge").textContent).toBe("Độ tin cậy thấp");
  });

  it("shows a neutral MEDIUM badge with no verified wording", () => {
    renderBubble(baseMessage({ confidence: "MEDIUM" }), true);
    expect(screen.getByTestId("confidence-badge").textContent).toBe("Độ tin cậy trung bình");
    // MEDIUM must not read as advisor-confirmed: no banner, no check icon.
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    expect(document.querySelector(".anticon-check")).toBeNull();
  });

  it("shows the HIGH badge without promising legal accuracy", () => {
    renderBubble(baseMessage({ confidence: "HIGH" }), true);
    const label = screen.getByTestId("confidence-badge").textContent ?? "";
    expect(label).toBe("Độ tin cậy cao");
    expect(label).not.toMatch(/đảm bảo pháp lý|chính xác 100%/i);
  });

  it("shows no badge before the done frame arrives", () => {
    renderBubble(baseMessage({ content: "Đang soạn", streaming: true }), true);
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
  });

  it("shows trace id and latency on a finished answer", () => {
    renderBubble(baseMessage({ traceId: "trace-abc", latencyMs: 1200 }), true);
    expect(screen.getByText(/trace_id: trace-abc/)).toBeTruthy();
    expect(screen.getByText(/phản hồi trong/)).toBeTruthy();
  });
});
