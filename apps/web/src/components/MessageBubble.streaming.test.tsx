// @vitest-environment jsdom
/**
 * Premium streaming UX contract (round 2):
 *  1. waiting phase (streaming, no content) shows the staged thinking label;
 *  2. streaming phase renders via the stream-safe AnswerBlocks path;
 *  3. non-interrupted retryable errors show the retry affordance;
 *  4. non-retryable non-interrupted errors render the styled error Alert
 *     (no retry button);
 *  5. finished assistant messages expose the copy button; clipboard write
 *     fires the "Đã sao chép" feedback.
 */
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MessageBubble, type ChatMessage } from "@/components/MessageBubble";

function baseMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    ...overrides,
  };
}

/** MessageBubble requires the antd App context for message.success feedback. */
function renderBubble(message: ChatMessage, onRetry?: (m: ChatMessage) => void) {
  return render(
    <AntApp>
      <MessageBubble message={message} onRetry={onRetry} />
    </AntApp>,
  );
}

beforeEach(() => {
  if (!window.matchMedia) {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  }
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MessageBubble thinking/typing states", () => {
  it("waiting phase (no content) shows the staged thinking label", () => {
    renderBubble(baseMessage({ streaming: true, acknowledged: true }));
    expect(screen.getByText(/Đang phân tích câu hỏi/)).toBeTruthy();
    expect(screen.getByText("AI đã nhận câu hỏi")).toBeTruthy();
  });

  it("thinking label advances with progressStep", () => {
    renderBubble(baseMessage({ streaming: true, acknowledged: true, progressStep: 1 }));
    expect(screen.getByText(/Đang tra cứu tài liệu pháp lý/)).toBeTruthy();
  });

  it("streaming phase with partial content renders the stream-safe path (no raw heading)", () => {
    const { container } = renderBubble(
      baseMessage({ streaming: true, content: "Giá căn 2PN như sau\n## Bảng giá" }),
    );
    // Unfinished heading stays plain text (no h* element, no literal # leak).
    expect(container.querySelector("h1,h2,h3,h4,h5,h6")).toBeNull();
    expect(container.textContent).not.toContain("#");
    expect(container.textContent).toContain("Bảng giá");
  });
});

describe("MessageBubble error UX", () => {
  it("retryable error frame shows the retry affordance with partial content kept", () => {
    const onRetry = vi.fn();
    renderBubble(
      baseMessage({
        content: "Căn 2PN có diện tích 72m2",
        error: true,
        retryable: true,
        retryQuery: "Giá 2PN?",
      }),
      onRetry,
    );
    expect(screen.getByText(/Căn 2PN có diện tích/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Thử lại/i })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Thử lại/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("non-retryable non-interrupted error renders the styled Alert without a retry button", () => {
    renderBubble(baseMessage({ content: "Yêu cầu không hợp lệ.", error: true }));
    expect(screen.getByText("Yêu cầu không hợp lệ.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Thử lại/i })).toBeNull();
    expect(screen.queryByText("Kết nối bị gián đoạn")).toBeNull();
  });
});

describe("MessageBubble copy affordance", () => {
  it("finished assistant message offers copy and confirms with Đã sao chép", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    renderBubble(baseMessage({ content: "Căn 2PN giá 3,2 tỷ." }));
    const button = screen.getByRole("button", { name: "Sao chép câu trả lời" });
    fireEvent.click(button);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Căn 2PN giá 3,2 tỷ."));
    await waitFor(() => expect(screen.getByText("Đã sao chép")).toBeTruthy());
  });

  it("streaming messages do not show the copy button yet", () => {
    renderBubble(baseMessage({ streaming: true, content: "Đang trả lời..." }));
    expect(screen.queryByRole("button", { name: "Sao chép câu trả lời" })).toBeNull();
  });
});
