import { describe, expect, it } from "vitest";
import {
  BOLD_PRICE_RE,
  boldPrice,
  classifyBlock,
  isTableBlock,
  parseTable,
  splitBlocks,
} from "./inline-format";

describe("boldPrice", () => {
  // boldPrice emits markdown `**...**` (not raw <strong>) so ReactMarkdown
  // v9 without rehype-raw actually parses it into a strong node.
  it("wraps VND amounts in markdown bold", () => {
    expect(boldPrice("Giá từ 2,1 tỷ/căn")).toContain("**2,1 tỷ**");
  });
  it("handles triệu and tỷ/m²", () => {
    expect(boldPrice("500 triệu")).toContain("**500 triệu**");
    expect(boldPrice("1.2 tỷ/m²")).toContain("**1.2 tỷ/m²**");
  });
  it("[RV-18/08] does NOT match tr output of trường", () => {
    expect(boldPrice("cách 5 trường học")).not.toContain("**");
  });
  it("leaves strings without prices untouched", () => {
    expect(boldPrice("hello world")).toBe("hello world");
  });
});

describe("classifyBlock", () => {
  it("detects a table block", () => {
    expect(classifyBlock("| A | B |\n|---|---|\n| 1 | 2 |")).toBe("table");
  });
  it("detects heading", () => {
    expect(classifyBlock("## Giá")).toBe("heading");
  });
  it("detects disclosure callout", () => {
    expect(classifyBlock("Định hướng: tham khảo pháp lý")).toBe("callout");
  });
  it("detects list", () => {
    expect(classifyBlock("- item a\n- item b")).toBe("list");
  });
  it("defaults to paragraph", () => {
    expect(classifyBlock("Câu hỏi đơn giản.")).toBe("paragraph");
  });
});

describe("parseTable", () => {
  it("parses header + body, skipping separator row", () => {
    const parsed = parseTable("| Khoản | Giá |\n|---|---|\n| Tiền cọc | 200 triệu |");
    expect(parsed?.header).toEqual(["Khoản", "Giá"]);
    expect(parsed?.rows).toEqual([["Tiền cọc", "200 triệu"]]);
  });
  it("returns null for non-table", () => {
    expect(parseTable("not a table")).toBeNull();
  });
  it("[streaming-safe] incomplete trailing table does not crash", () => {
    expect(parseTable("| cột |\n| cột 2 |")).not.toBeNull();
  });
});

describe("splitBlocks", () => {
  it("splits on blank lines and drops empties", () => {
    expect(splitBlocks("## Giá\n\n500 triệu\n\n- a\n- b")).toHaveLength(3);
  });
  it("handles streaming tail without trailing newline", () => {
    expect(splitBlocks("## Giá\n\n| cột |").length).toBeGreaterThanOrEqual(1);
  });
});

describe("BOLD_PRICE_RE negative guards", () => {
  it("does not match trailing Vietnamese letters after tr", () => {
    expect("5 trường học".match(BOLD_PRICE_RE)).toBeNull();
  });
  it("matches standalone tr unit", () => {
    expect("3 tr".match(BOLD_PRICE_RE)?.[0]).toContain("3 tr");
  });
});