// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarkdownView } from "./MarkdownView";

afterEach(() => cleanup());

const TABLE_CONTENT = [
  "Giá bán:",
  "| Căn | Giá |",
  "|---|---|",
  "| A01 | 2,1 tỷ |",
].join("\n");

function spyConsoleError() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}

describe("[ISSUE-4] MarkdownView wrapper + table safety", () => {
  it("wraps content in a plain div instead of antd Typography", () => {
    const { container } = render(<MarkdownView content={TABLE_CONTENT} />);
    const root = container.firstElementChild;
    expect(root?.tagName).toBe("DIV");
    expect(root?.className).not.toContain("ant-typography");
  });

  it("renders GFM tables without nesting them in a paragraph", () => {
    const err = spyConsoleError();
    const { container } = render(<MarkdownView content={TABLE_CONTENT} />);
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector("table")?.closest("p")).toBeNull();
    expect(
      err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))
    ).toBe(false);
    err.mockRestore();
  });

  it("output is stable across renders (SSR/client determinism)", () => {
    const first = render(<MarkdownView content={TABLE_CONTENT} />);
    const html1 = first.container.innerHTML;
    cleanup();
    const second = render(<MarkdownView content={TABLE_CONTENT} />);
    expect(second.container.innerHTML).toBe(html1);
  });
});
