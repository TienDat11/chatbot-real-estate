// @vitest-environment jsdom
/**
 * Streaming UX contract:
 *  1. one compact progress surface remains visible for the full stream;
 *  2. sources are stored early but rendered only after completion;
 *  3. completed answers place sources after answer/media/copy content.
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

describe("MessageBubble streaming progress", () => {
  it("starts collapsed with an icon-only disclosure control", () => {
    renderBubble(baseMessage({ streaming: true, progressStep: 0 }));
    expect(screen.getByText("Đang hiểu yêu cầu")).toBeTruthy();
    expect(screen.queryByText("Hiểu yêu cầu")).toBeNull();
    const button = screen.getByRole("button", { name: "Mở tiến trình" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button.textContent).toBe("");
  });

  it("allows manual expand and collapse", () => {
    renderBubble(baseMessage({ streaming: true, progressStep: 2 }));
    const button = screen.getByRole("button", { name: "Mở tiến trình" });
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "Thu gọn tiến trình" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Hiểu yêu cầu")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Thu gọn tiến trình" }));
    expect(screen.getByRole("button", { name: "Mở tiến trình" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Hiểu yêu cầu")).toBeNull();
  });

  it("does not change manual expansion when content starts streaming", () => {
    const view = renderBubble(baseMessage({ streaming: true, content: "", progressStep: 0 }));
    fireEvent.click(screen.getByRole("button", { name: "Mở tiến trình" }));
    view.rerender(<AntApp><MessageBubble message={baseMessage({ streaming: true, content: "Đầu", progressStep: 3 })} /></AntApp>);
    expect(screen.getByRole("button", { name: "Thu gọn tiến trình" })).toHaveAttribute("aria-expanded", "true");
  });

  it("keeps the compact progress header visible with partial answer content", () => {
    renderBubble(baseMessage({ streaming: true, content: "Một phần câu trả lời", progressStep: 3 }));
    expect(screen.getByText("Đang soạn câu trả lời")).toBeTruthy();
    expect(screen.getByText("Một phần câu trả lời")).toBeTruthy();
  });

  it("keeps partial markdown stream-safe while progress remains visible", () => {
    const { container } = renderBubble(
      baseMessage({ streaming: true, content: "Giá căn 2PN như sau\n## Bảng giá", progressStep: 3 }),
    );
    expect(container.querySelector("h1,h2,h3,h4,h5,h6")).toBeNull();
    expect(container.textContent).not.toContain("#");
    expect(container.textContent).toContain("Bảng giá");
  });


  it("hides progress after completion", () => {
    const view = renderBubble(baseMessage({ streaming: true, progressStep: 0 }));
    view.rerender(<AntApp><MessageBubble message={baseMessage({ streaming: false, content: "Hoàn tất" })} /></AntApp>);
    expect(screen.queryByRole("region", { name: "Tiến trình trả lời" })).toBeNull();
  });
});

describe("MessageBubble source placement", () => {
  const sources = [{ doc_id: "doc-1", title: "Tài liệu tham khảo", kind: "training" }];

  it("defers sources while the answer is streaming", () => {
    renderBubble(baseMessage({ streaming: true, content: "Đang trả lời", sources }));
    expect(screen.getByText("Đang trả lời")).toBeTruthy();
    expect(screen.queryByText("Nguồn tài liệu")).toBeNull();
    expect(screen.queryByText("Tài liệu tham khảo")).toBeNull();
  });

  it("renders sources after the completed answer", () => {
    const { container } = renderBubble(baseMessage({ streaming: false, content: "Câu trả lời cuối", sources }));
    expect(screen.getByText("Câu trả lời cuối")).toBeTruthy();
    expect(screen.getByText("Tài liệu tham khảo")).toBeTruthy();
    const answer = container.textContent!.indexOf("Câu trả lời cuối");
    const source = container.textContent!.indexOf("Tài liệu tham khảo");
    expect(answer).toBeLessThan(source);
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
