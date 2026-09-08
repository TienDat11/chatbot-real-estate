// @vitest-environment jsdom
/**
 * Resilience UX contract for interrupted streams (mid-stream cut before the
 * `done` frame, e.g. the dev proxy severing a slow SSE):
 *  1. an interrupted assistant message keeps its PARTIAL text visible and
 *     renders the "Kết nối bị gián đoạn" Alert with a "Thử lại" button;
 *  2. clicking "Thử lại" fires the onRetry hook with that message;
 *  3. a plain (non-interrupted) error message still renders the legacy red
 *     error line — no regression for TrainWorkspace-style consumers.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MessageBubble, type ChatMessage } from "@/components/MessageBubble";

function baseMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("MessageBubble interrupted-stream resilience", () => {
  it("keeps partial text and shows the retry Alert on an interrupted stream", () => {
    render(
      <MessageBubble
        message={baseMessage({
          content: "Căn 2PN có diện tích",
          error: true,
          interrupted: true,
          retryQuery: "Giá 2PN?",
        })}
      />
    );

    // Partial tokens survive the failure.
    expect(screen.getByText(/Căn 2PN có diện tích/)).toBeTruthy();
    // The retry affordance is present.
    expect(screen.getByText("Kết nối bị gián đoạn")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Thử lại/i })).toBeTruthy();
  });

  it("shows the error copy instead of an empty bubble when no tokens arrived", () => {
    render(
      <MessageBubble
        message={baseMessage({
          content: "Lỗi khi đọc luồng phản hồi.",
          error: true,
          interrupted: true,
          retryQuery: "Giá 2PN?",
        })}
      />
    );

    // No silent empty state: the error copy fills the bubble.
    expect(screen.getByText("Lỗi khi đọc luồng phản hồi.")).toBeTruthy();
    expect(screen.getByText("Kết nối bị gián đoạn")).toBeTruthy();
  });

  it('re-sends the same question when "Thử lại" is clicked', () => {
    const onRetry = vi.fn();
    const message = baseMessage({
      content: "Căn 2PN",
      error: true,
      interrupted: true,
      retryQuery: "Giá 2PN?",
    });
    render(<MessageBubble message={message} onRetry={onRetry} />);

    fireEvent.click(screen.getByRole("button", { name: /Thử lại/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledWith(message);
  });

  it("keeps the legacy red error line for non-interrupted errors", () => {
    render(
      <MessageBubble
        message={baseMessage({
          content: "Máy chủ trả lỗi 500.",
          error: true,
        })}
      />
    );

    expect(screen.getByText("Máy chủ trả lỗi 500.")).toBeTruthy();
    // No retry affordance outside the interrupted contract.
    expect(screen.queryByRole("button", { name: /Thử lại/i })).toBeNull();
    expect(screen.queryByText("Kết nối bị gián đoạn")).toBeNull();
  });
});
