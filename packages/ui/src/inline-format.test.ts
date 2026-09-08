import { describe, expect, it } from "vitest";
import {
  BOLD_PRICE_RE,
  boldPrice,
  canRenderAsP,
  classifyBlock,
  isDividerBlock,
  isTableBlock,
  normalizeBrTags,
  parseTable,
  partitionStreamedBlocks,
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
  it("does not treat ~ as inline-code opener (Vietnamese 'approximately')", () => {
    const out = boldPrice("giá ~2 tỷ, đợt 2 ~500 triệu");
    expect(out).toContain("**2 tỷ**");
    expect(out).toContain("**500 triệu**");
  });
});

describe("classifyBlock", () => {
  it("detects a table block", () => {
    expect(classifyBlock("| A | B |\n|---|---|\n| 1 | 2 |")).toBe("table");
  });
  it("detects heading", () => {
    expect(classifyBlock("## Giá")).toBe("heading");
  });
  it("[B1] detects thematic break as divider, for every marker character", () => {
    expect(classifyBlock("---")).toBe("divider");
    expect(classifyBlock("***")).toBe("divider");
    expect(classifyBlock("___")).toBe("divider");
    expect(classifyBlock("- - -")).toBe("divider");
    expect(classifyBlock("-----------")).toBe("divider");
  });
  it("[B1] prose containing a dash is NOT a divider", () => {
    expect(classifyBlock("Giá từ - 3 đến - 5 tỷ")).toBe("paragraph");
    // A hyphen-minus hyphen-minus inside a sentence is prose.
    expect(classifyBlock("Căn A - 2PN")).toBe("paragraph");
  });
  it("[B1] prose pipe line over a rule stays paragraph (table check wins)", () => {
    expect(classifyBlock("Tiêu chí A | Tiêu chí B\n---")).toBe("paragraph");
  });
  it("[B1] isDividerBlock is exact", () => {
    expect(isDividerBlock("---")).toBe(true);
    expect(isDividerBlock("--")).toBe(false);
    expect(isDividerBlock("---\nprose")).toBe(false);
  });
  it("[B2] <br> inside a table row becomes a separator so the table stays one line", () => {
    const row = "| Gói | Ưu đãi |\n|---|---|\n| Sớm | • 3,91-4,67 tỷ<br>• 4,31-5,16 tỷ |";
    const blocks = splitBlocks(row);
    expect(blocks).toHaveLength(1);
    expect(isTableBlock(blocks[0])).toBe(true);
    expect(blocks[0]).not.toContain("<br");
    expect(blocks[0]).toContain("•");
    expect(blocks[0].split("\n")).toHaveLength(3);
  });
  it("[B2] <br> in prose becomes real newlines (all case/spacing variants)", () => {
    expect(normalizeBrTags("dòng 1<br>dòng 2")).toBe("dòng 1\ndòng 2");
    expect(normalizeBrTags("dòng 1<BR>dòng 2")).toBe("dòng 1\ndòng 2");
    expect(normalizeBrTags("dòng 1<br/>dòng 2")).toBe("dòng 1\ndòng 2");
    expect(normalizeBrTags("dòng 1<br />dòng 2")).toBe("dòng 1\ndòng 2");
  });
  it("[B2] normalized <br> splits prose into separate blocks/lines", () => {
    const blocks = splitBlocks("• 3,91-4,67 tỷ (Sớm 95%)<br>• 4,31-5,16 tỷ (TT Chuẩn)");
    // <br> becomes \n inside ONE block (soft line break, like markdown);
    // the point is no literal tag survives, not paragraph splitting.
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toContain("3,91-4,67");
    expect(blocks[0]).toContain("4,31-5,16");
    expect(blocks.join("\n")).not.toContain("<br");
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
  it("[streaming-safe] two pipe rows without a delimiter row are prose, not a table", () => {
    // No separator row -> remark-gfm renders this as paragraph text, so the
    // safe classification is null (never a bogus single-column table).
    expect(parseTable("| cột |\n| cột 2 |")).toBeNull();
    // A partial run whose delimiter row HAS arrived must still parse, even
    // when the in-flight last row is missing its trailing pipe.
    expect(parseTable("| cột |\n|---|\n| giá tham khảo")).not.toBeNull();
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

describe("[ISSUE-4] splitBlocks splits GFM tables at block boundary", () => {
  it("separates prose from a table sharing the same block (no blank line)", () => {
    const blocks = splitBlocks("Bảng giá như sau:\n| Khoản | Giá |\n|---|---|\n| Cọc | 200 triệu |");
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toBe("Bảng giá như sau:");
    expect(isTableBlock(blocks[1])).toBe(true);
  });

  it("keeps a pure table as exactly one block", () => {
    const table = "| A | B |\n|---|---|\n| 1 | 2 |";
    expect(splitBlocks(table)).toEqual([table]);
  });

  it("splits leading text, table, and trailing text into three blocks", () => {
    const blocks = splitBlocks(
      "Giá tham khảo:\n| Loại | Giá |\n|---|---|\n| Căn A | 2 tỷ |\nVui lòng liên hệ sales."
    );
    expect(blocks).toEqual(["Giá tham khảo:", "| Loại | Giá |\n|---|---|\n| Căn A | 2 tỷ |", "Vui lòng liên hệ sales."]);
  });

  it("leaves plain prose untouched as a single block", () => {
    expect(splitBlocks("Một đoạn văn\nxuống dòng đơn giản.")).toHaveLength(1);
  });

  it("does not treat pipe lines without a separator row as a table", () => {
    expect(splitBlocks("text\n| chỉ một cột |\n| dòng khác |")).toHaveLength(1);
  });
});

describe("[ISSUE-4] classifyBlock never leaves a table inside a paragraph", () => {
  const mixed = "intro\n| A | B |\n|---|---|\n| 1 | 2 |";
  it("mixed prose+table classifies as table, never paragraph", () => {
    expect(classifyBlock(mixed)).toBe("table");
    expect(classifyBlock(mixed)).not.toBe("paragraph");
  });
  it("prose with unrelated pipes stays a paragraph", () => {
    expect(classifyBlock("Ký hiệu | nghĩa khác nhau")).toBe("paragraph");
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

describe("[R1] streaming-partial tables never classify paragraph", () => {
  // Exact pattern from SSE logs: in-flight final row whose trailing pipe has
  // not been flushed yet.
  const PARTIAL_TAIL = "| Khoản | Giá |\n|---|---|\n| Đặt cọc | 200 triệu";

  // GFM also allows delimiter-style tables without any leading/trailing pipes.
  const NO_PIPES = "Căn | Giá\n--- | ---\n2PN | 3,2 tỷ";

  it("header + separator + unpiped trailing row is a table", () => {
    expect(classifyBlock(PARTIAL_TAIL)).toBe("table");
    expect(isTableBlock(PARTIAL_TAIL)).toBe(true);
    expect(parseTable(PARTIAL_TAIL)?.rows).toEqual([["Đặt cọc", "200 triệu"]]);
  });

  it("delimiter-style table without leading pipes is a table", () => {
    expect(classifyBlock(NO_PIPES)).toBe("table");
    expect(isTableBlock(NO_PIPES)).toBe(true);
    expect(parseTable(NO_PIPES)?.header).toEqual(["Căn", "Giá"]);
  });

  it("streaming accumulation: block flips prose -> table exactly when separator lands", () => {
    const chunks = [
      "| Căn | Giá |",
      "| Căn | Giá |\n|---|---|",
      "| Căn | Giá |\n|---|---|\n| 2PN |",
      "| Căn | Giá |\n|---|---|\n| 2PN | 3,2 tỷ",
    ];
    expect(classifyBlock(chunks[0])).not.toBe("table"); // no separator yet: safe prose
    for (const partial of chunks.slice(1)) {
      expect(classifyBlock(partial)).toBe("table");
      expect(classifyBlock(partial)).not.toBe("paragraph");
    }
  });

  it("prose pipe line over a mismatched rule stays a paragraph", () => {
    expect(classifyBlock("Tiêu chí A | Tiêu chí B\n---")).toBe("paragraph");
  });
});

describe("[R1] list blocks with disclosure keywords", () => {
  it("keyword bullets classify as list, not callout/paragraph", () => {
    const block = "- Lưu ý: giá tham khảo từ sales\n- 2PN: 3,2 tỷ";
    expect(classifyBlock(block)).toBe("list");
  });
  it("plain keyword sentence still classifies as callout", () => {
    expect(classifyBlock("Lưu ý: giá chính thức từ sales.")).toBe("callout");
  });
});

describe("[R1] canRenderAsP guard", () => {
  it("rejects blocks containing list/heading/table/fence/html lines", () => {
    expect(canRenderAsP("- lưu ý item")).toBe(false);
    expect(canRenderAsP("intro line\n## Heading giữa chừng")).toBe(false);
    expect(canRenderAsP("intro\n| A | B |\n|---|---|\n| 1 | 2 |")).toBe(false);
    expect(canRenderAsP("văn bản\n```code```")).toBe(false);
    expect(canRenderAsP("<div>x</div>")).toBe(false);
  });
  it("[B1] rejects blocks containing a thematic break line (hr-in-p guard)", () => {
    expect(canRenderAsP("prose\n---")).toBe(false);
    expect(canRenderAsP("***")).toBe(false);
  });
  it("accepts proven inline-only prose", () => {
    expect(canRenderAsP("Giá từ **2,1 tỷ** cho căn 2PN.")).toBe(true);
  });
});

describe("[B1] streaming divider vs pending tail", () => {
  it("a complete --- line is a stable divider block", () => {
    const { stable, pending } = partitionStreamedBlocks("Phần trên.\n\n---\n\nPhần dưới.");
    expect(stable).toEqual(["Phần trên.", "---", "Phần dưới."]);
    expect(pending).toBeNull();
    expect(classifyBlock(stable[1])).toBe("divider");
  });
  it("a trailing last-block --- line is still stable (break is self-terminating)", () => {
    const { stable } = partitionStreamedBlocks("Phần trên.\n\n---");
    expect(stable).toEqual(["Phần trên.", "---"]);
  });
  it("an incomplete tail `--` stays pending, never a divider", () => {
    const { stable, pending } = partitionStreamedBlocks("Phần trên.\n\n--");
    expect(stable).toEqual(["Phần trên."]);
    expect(pending).toBe("--");
  });
  it("[B3] unclosed ## heading + body: heading stays pending until body lands", () => {
    const mid = partitionStreamedBlocks("Chính sách ưu đãi & Phương thức thanh toán\n## So sánh");
    expect(mid.stable).toEqual([]);
    expect(mid.pending).toBe("Chính sách ưu đãi & Phương thức thanh toán\n## So sánh");
    const done = partitionStreamedBlocks("Chính sách ưu đãi & Phương thức thanh toán\n\n## So sánh\nNội dung chi tiết.");
    expect(done.stable).toHaveLength(2);
    expect(classifyBlock(done.stable[1])).toBe("heading");
  });
});

describe("[C1] fence-aware streaming partition", () => {
  it("an open fence with a blank line inside stays pending (never stable fragments)", () => {
    // splitBlocks splits on the blank line INSIDE the fence; the partition
    // must fold the fragments back into one pending tail instead of
    // promoting them to stable blocks (which would mount <pre> mid-stream).
    const open = "```mermaid\n\ngraph TD\n  A --> B";
    const { stable, pending } = partitionStreamedBlocks(`Phía trên.\n\n${open}`);
    expect(stable).toEqual(["Phía trên."]);
    expect(pending).toBe(open);
  });
  it("a fence whose closing line arrives promotes the folded region as ONE stable block", () => {
    const fence = "```mermaid\n\ngraph TD\n  A --> B\n```";
    const { stable, pending } = partitionStreamedBlocks(`Phía trên.\n\n${fence}`);
    expect(pending).toBeNull();
    expect(stable).toEqual(["Phía trên.", fence]);
    expect(classifyBlock(stable[1])).toBe("code");
  });
  it("a self-contained fence inside one block promotes directly", () => {
    const { stable, pending } = partitionStreamedBlocks("```sql\nSELECT 1;\n```");
    expect(pending).toBeNull();
    expect(stable).toEqual(["```sql\nSELECT 1;\n```"]);
    expect(classifyBlock(stable[0])).toBe("code");
  });
});

describe("[C1] classifyBlock: closed fenced code", () => {
  it("closed fences classify as code regardless of inner #/|/- shapes", () => {
    expect(classifyBlock("```mermaid\n\ngraph TD\n  A --> B\n```")).toBe("code");
    // Inner content that looks like other block kinds must not misclassify.
    expect(classifyBlock("```\n# not a heading\n| not | a | table |\n- not a list\n```")).toBe("code");
  });
  it("an opening fence alone is NOT code (still growing)", () => {
    expect(classifyBlock("```mermaid")).not.toBe("code");
  });
});