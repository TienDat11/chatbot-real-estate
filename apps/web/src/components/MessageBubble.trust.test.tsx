// @vitest-environment jsdom
/**
 * Trust-safety UI contract (W1-05): the backend's confidence/review signals
 * must be visible on the bubble, in every mode and lifecycle state:
 *  1. requires_review=true renders the ReviewBanner ALWAYS — finished,
 *     streaming, or errored (never suppressed by layout state);
 *  2. requires_review absent/false renders no banner;
 *  3. a finished answer shows the 3-tier ConfidenceBadge for its confidence;
 *  4. a still-streaming answer shows no badge (confidence only lands on the
 *     done frame).
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

function renderBubble(message: ChatMessage) {
  return render(
    <AntApp>
      <MessageBubble message={message} />
    </AntApp>,
  );
}

afterEach(() => {
  cleanup();
});

describe("MessageBubble review banner (requires_review)", () => {
  it("renders the banner on a finished flagged answer", () => {
    renderBubble(baseMessage({ requires_review: true, confidence: "LOW", traceId: "t1" }));
    expect(screen.getByText(BANNER_HEADING)).toBeTruthy();
  });

  it("renders the banner even while the answer is still streaming", () => {
    renderBubble(baseMessage({ content: "Đang soạn", streaming: true, requires_review: true }));
    expect(screen.getByText(BANNER_HEADING)).toBeTruthy();
  });

  it("renders the banner alongside a plain error message", () => {
    renderBubble(baseMessage({ content: "Máy chủ trả lỗi 500.", error: true, requires_review: true }));
    expect(screen.getByText(BANNER_HEADING)).toBeTruthy();
    expect(screen.getByText("Máy chủ trả lỗi 500.")).toBeTruthy();
  });

  it("renders no banner when the flag is false or absent", () => {
    renderBubble(baseMessage({ requires_review: false, confidence: "HIGH" }));
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    cleanup();
    renderBubble(baseMessage({ confidence: "HIGH" }));
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
  });
});

describe("MessageBubble confidence badge", () => {
  it("shows the red LOW badge as an explicit warning", () => {
    renderBubble(baseMessage({ confidence: "LOW" }));
    expect(screen.getByTestId("confidence-badge").textContent).toBe("Độ tin cậy thấp");
  });

  it("shows a neutral MEDIUM badge with no verified wording", () => {
    renderBubble(baseMessage({ confidence: "MEDIUM" }));
    expect(screen.getByTestId("confidence-badge").textContent).toBe("Độ tin cậy trung bình");
    // MEDIUM must not read as advisor-confirmed: no banner, no check icon.
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    expect(document.querySelector(".anticon-check")).toBeNull();
  });

  it("shows the HIGH badge without promising legal accuracy", () => {
    renderBubble(baseMessage({ confidence: "HIGH" }));
    const label = screen.getByTestId("confidence-badge").textContent ?? "";
    expect(label).toBe("Độ tin cậy cao");
    expect(label).not.toMatch(/đảm bảo pháp lý|chính xác 100%/i);
  });

  it("shows no badge before the done frame arrives", () => {
    renderBubble(baseMessage({ content: "Đang soạn", streaming: true }));
    expect(screen.queryByTestId("confidence-badge")).toBeNull();
  });
});
