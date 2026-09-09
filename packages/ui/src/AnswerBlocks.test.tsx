// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnswerBlocks } from "./AnswerBlocks";

afterEach(() => cleanup());

/** Prose and GFM table emitted inside ONE block (no blank line between). */
const MIXED_CONTENT = [
  "Bảng giá căn 2 phòng ngủ như sau:",
  "| Khoản | Giá |",
  "|---|---|",
  "| Đặt cọc | 200 triệu |",
  "| Thanh toán đợt 1 | 10% |",
  "Vui lòng liên hệ sales để được tư vấn thêm.",
].join("\n");

function spyConsoleError() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}

describe("[R1] render never nests block constructs under p", () => {
  it("renders a partial SSE table (missing trailing pipe) as a real table outside <p>", () => {
    const err = spyConsoleError();
    const content = ["Giá căn như sau:", "| Khoản | Giá |", "|---|---|", "| Đặt cọc | 200 triệu"].join("\n");
    const { container } = render(<AnswerBlocks content={content} />);
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.closest("p")).toBeNull();
    expect(document.querySelector("p table")).toBeNull();
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });

  it("renders a delimiter-style GFM table without edge pipes outside <p>", () => {
    const err = spyConsoleError();
    const content = ["Bảng so sánh:", "Căn | Diện tích | Giá", "--- | --- | ---", "Studio | 35m² | 2,4 tỷ", "2PN | 65m² | 3,2 tỷ"].join("\n");
    const { container } = render(<AnswerBlocks content={content} />);
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector("p table")).toBeNull();
    expect(container.querySelector("table")?.closest("p")).toBeNull();
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });

  it("list lines containing disclosure keywords render a ul, never ul-in-p", () => {
    const err = spyConsoleError();
    const content = "- Lưu ý: giá tham khảo từ sales\n- Chính sách thanh toán: đợt 1 là 10%";
    const { container } = render(<AnswerBlocks content={content} />);
    const list = container.querySelector("ul");
    expect(list).not.toBeNull();
    expect(list?.closest("p")).toBeNull();
    expect(document.querySelector("p ul, p ol")).toBeNull();
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });

  it("an interrupted heading inside prose takes the neutral container, not h*-in-p", () => {
    const err = spyConsoleError();
    const content = "Phần giá như sau\n## Bảng giá tham khảo";
    const { container } = render(<AnswerBlocks content={content} />);
    expect(document.querySelector("p h1, p h2, p h3, p h4, p h5, p h6")).toBeNull();
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });
});

describe("[R1] streaming accumulation across SSE chunks", () => {
  it("every prefix of a streamed table stays valid (no nesting errors)", () => {
    const err = spyConsoleError();
    const chunks = [
      "Bảng giá:",
      "Bảng giá:\n| Căn | Giá |",
      "Bảng giá:\n| Căn | Giá |\n|---|---|",
      "Bảng giá:\n| Căn | Giá |\n|---|---|\n| 2PN |",
      "Bảng giá:\n| Căn | Giá |\n|---|---|\n| 2PN | 3,2 tỷ",
    ];
    let last: ReturnType<typeof render> | null = null;
    for (const content of chunks) {
      cleanup();
      last = render(<AnswerBlocks key={content} content={content} />);
      expect(last.container.querySelector("p table")).toBeNull();
      expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    }
    // Final complete state still shows the intro paragraph and the table.
    expect(last!.container.querySelector("table")).not.toBeNull();
    err.mockRestore();
  });
});

describe("[stream-safe] streaming prop suppresses raw markdown symbols", () => {
  it("mid-stream: unclosed heading, table, list and bold render as plain text (no block constructs)", () => {
    const err = spyConsoleError();
    // Simulate the exact mid-stream states the issue pins: heading without
    // body, incomplete table, list item in progress, unclosed bold.
    const partials = [
      "## Bảng giá căn",
      "Bảng giá:\n| Căn | Giá |",
      "- Vị trí:",
      "Giá chỉ từ **3,2 tỷ",
    ];
    for (const content of partials) {
      cleanup();
      const { container } = render(<AnswerBlocks key={content} content={content} streaming />);
      // The raw text IS shown (nothing hidden from the reader)...
      expect(container.textContent!.length).toBeGreaterThan(0);
      // ...but no block-level markdown construct materialized mid-stream.
      expect(container.querySelector("table")).toBeNull();
      expect(container.querySelector("h1,h2,h3,h4,h5,h6")).toBeNull();
      expect(container.querySelector("ul,ol")).toBeNull();
      expect(container.querySelector("strong")).toBeNull();
    }
    err.mockRestore();
  });

  it("mid-stream: no literal '#' or '|' glyphs leak from incomplete blocks", () => {
    const { container } = render(
      <AnswerBlocks content={"Bảng giá căn\n| Căn | Giá |"} streaming />,
    );
    expect(container.textContent).not.toContain("|");
    const { container: c2 } = render(<AnswerBlocks content="## Giá" streaming />);
    expect(c2.textContent).not.toContain("#");
  });

  it("completed mid-stream table renders as a real table once the delimiter + rows land", () => {
    const { container } = render(
      <AnswerBlocks
        content={"Bảng giá:\n| Căn | Giá |\n|---|---|\n| 2PN | 3,2 tỷ |"}
        streaming
      />,
    );
    expect(container.querySelector("table")).not.toBeNull();
  });

  it("at done (streaming=false) rendering is identical to the legacy path", () => {
    const content = ["## Bảng giá", "Bảng giá:\n| Căn | Giá |\n|---|---|\n| 2PN | 3,2 tỷ |", "- Lưu ý: giá tham khảo"].join("\n\n");
    const { container } = render(<AnswerBlocks content={content} />);
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector("ul")).not.toBeNull();
    expect(container.textContent).toContain("Bảng giá");
  });

  it("streams heading + body: a heading WITH body text renders as markdown mid-stream", () => {
    const { container } = render(
      <AnswerBlocks content={"## Bảng giá\nNội dung dưới heading."} streaming />,
    );
    // Heading has a body line -> terminated -> renders through markdown path.
    expect(container.textContent).toContain("Bảng giá");
    expect(container.textContent).toContain("Nội dung dưới heading.");
  });
});

describe("[B1] thematic break renders as a divider, never hr-in-p", () => {
  it("a --- line renders a divider element outside <p> (legacy path)", () => {
    const err = spyConsoleError();
    const { container } = render(<AnswerBlocks content={"Trên dòng kẻ.\n\n---\n\nDưới dòng kẻ."} />);
    const divider = container.querySelector(".rag-answer__divider");
    expect(divider).not.toBeNull();
    expect(divider!.closest("p")).toBeNull();
    expect(document.querySelector("p hr, hr")).toBeNull();
    // Both prose lines survive as paragraphs.
    expect(container.textContent).toContain("Trên dòng kẻ.");
    expect(container.textContent).toContain("Dưới dòng kẻ.");
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });

  it("every thematic-break marker form renders a divider", () => {
    for (const marker of ["---", "***", "___", "- - -"]) {
      cleanup();
      const { container } = render(<AnswerBlocks key={marker} content={`A\n\n${marker}\n\nB`} />);
      expect(container.querySelector(".rag-answer__divider")).not.toBeNull();
    }
  });

  it("streaming: a complete --- line already renders as a divider", () => {
    const { container } = render(<AnswerBlocks content={"Phần trên.\n\n---"} streaming />);
    expect(container.querySelector(".rag-answer__divider")).not.toBeNull();
  });

  it("streaming: an incomplete `--` tail stays pending plain text (no divider yet)", () => {
    const { container } = render(<AnswerBlocks content={"Phần trên.\n\n--"} streaming />);
    expect(container.querySelector(".rag-answer__divider")).toBeNull();
    expect(container.textContent).toContain("Phần trên.");
  });

  it("a paragraph containing a stray --- line takes the neutral div, not p>hr", () => {
    const err = spyConsoleError();
    const { container } = render(<AnswerBlocks content={"Văn bản thường\n---\nVẫn đoạn văn."} />);
    expect(container.querySelector("hr")).toBeNull();
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });
});

describe("[B2] literal <br> never leaks as visible text", () => {
  it("bullets separated by <br> render as real list items", () => {
    const { container } = render(
      <AnswerBlocks content={"• 3,91-4,67 tỷ (Sớm 95%)<br>• 4,31-5,16 tỷ (TT Chuẩn)<br>• 5,0 tỷ (VA)"} />,
    );
    expect(container.textContent).not.toContain("<br");
    expect(container.textContent).toContain("3,91-4,67 tỷ");
    expect(container.textContent).toContain("4,31-5,16 tỷ");
  });

  it("<br> inside a table cell keeps the table intact with an in-cell separator", () => {
    const err = spyConsoleError();
    const content = [
      "| Gói | Ưu đãi |",
      "|---|---|",
      "| Sớm | • 3,91-4,67 tỷ<br>• 4,31-5,16 tỷ |",
    ].join("\n");
    const { container } = render(<AnswerBlocks content={content} />);
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector("table")!.closest("p")).toBeNull();
    expect(container.textContent).not.toContain("<br");
    // The cell stays a single line: bullet separator, not a newline.
    expect(container.textContent).toContain("tỷ • 4,31-5,16");
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });

  it("uppercase and self-closing <BR/> variants are normalized too", () => {
    const { container } = render(<AnswerBlocks content={"A<BR/>B<Br >C"} />);
    expect(container.textContent).not.toContain("<BR");
    expect(container.textContent).not.toContain("<Br");
  });
});

describe("[B3] font clamp — heading capped, body scale everywhere", () => {
  it("heading blocks carry the capped class and capped computed size", () => {
    const { container } = render(<AnswerBlocks content={"## So sánh phương án\nNội dung."} />);
    const heading = container.querySelector(".rag-answer__heading")!;
    expect(heading).not.toBeNull();
    // Inline style pins the cap: calc(var(--fs-body) * 1.15) at most.
    const style = (heading as HTMLElement).style.fontSize;
    expect(style).toContain("calc(var(--fs-body");
    expect(style).toContain("1.15");
    expect(container.textContent).toContain("So sánh phương án");
  });

  it("long paragraphs after a heading stay on the body scale", () => {
    const longBody = "Chính sách ưu đãi kéo dài ".repeat(30);
    const { container } = render(<AnswerBlocks content={`## So sánh\n\n${longBody}`} />);
    const para = Array.from(container.querySelectorAll("p")).find(
      (el) => el.textContent === longBody.trim(),
    )!;
    expect(para).toBeTruthy();
    expect((para as HTMLElement).style.fontSize).toContain("var(--fs-body");
  });

  it("heading followed by body WITHOUT blank line renders body at body scale, not heading scale (B3 root cause)", () => {
    const longBody = "Chính sách ưu đãi kéo dài cho toàn dự án. ".repeat(20);
    const { container } = render(
      <AnswerBlocks content={`## So sánh phương án\n${longBody}`} />,
    );
    // Heading line gets the capped class...
    expect(container.querySelector(".rag-answer__heading")?.textContent).toContain("So sánh phương án");
    // ...but the body does NOT inherit heading typography: it renders as its
    // own paragraph with the body font token.
    const paras = Array.from(container.querySelectorAll("p"));
    expect(paras.some((p) => p.textContent === longBody.trim())).toBe(true);
    expect((paras.find((p) => p.textContent === longBody.trim()) as HTMLElement).style.fontSize).toContain("var(--fs-body");
  });

  it("heading followed by body WITHOUT blank line stays valid for hydration", () => {
    const err = spyConsoleError();
    render(<AnswerBlocks content={"Intro\n## So sánh\nThân bài dài sau heading."} />);
    expect(document.querySelector("p h1, p h2, p h3, p h4, p h5, p h6")).toBeNull();
    expect(err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))).toBe(false);
    err.mockRestore();
  });

  it("streaming pending tail inherits the body size", () => {
    const { container } = render(
      <AnswerBlocks content={"Chính sách ưu đãi & Phương thức thanh toán\n## So sánh"} streaming />,
    );
    const tail = container.querySelector(".rag-answer__pending") as HTMLElement;
    expect(tail).not.toBeNull();
    expect(tail.style.fontSize).toContain("var(--fs-body");
  });

  it("root bubble carries the rag-answer clamp class", () => {
    const { container } = render(<AnswerBlocks content="Văn bản." />);
    expect(container.querySelector(".rag-answer")).not.toBeNull();
  });
});

describe("[ISSUE-4] AnswerBlocks never nests a GFM table in a paragraph", () => {
  it("splits mixed content so <table> is never a descendant of <p>", () => {
    const err = spyConsoleError();
    const { container } = render(<AnswerBlocks content={MIXED_CONTENT} />);
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.closest("p")).toBeNull();
    // Intro prose still renders as its own paragraph.
    const paragraphs = Array.from(container.querySelectorAll("p"));
    expect(paragraphs.some((p) => p.textContent?.includes("Bảng giá"))).toBe(true);
    expect(
      err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))
    ).toBe(false);
    err.mockRestore();
  });

  it("produces no validateDOMNesting error for table-heavy content", () => {
    const err = spyConsoleError();
    render(<AnswerBlocks content={MIXED_CONTENT + "\n\n" + MIXED_CONTENT} />);
    expect(
      err.mock.calls.some((c) => String(c[0]).includes("validateDOMNesting"))
    ).toBe(false);
    expect(
      err.mock.calls.some((c) => String(c[0]).includes("<p>"))
    ).toBe(false);
    err.mockRestore();
  });
});

describe("[C1] fenced code streaming", () => {
  it("mid-stream: an open fenced block (blank line inside) renders as plain text, NO pre/code", () => {
    const err = spyConsoleError();
    // Exactly the issue's symptom: ` ```mermaid ` + blank line + content.
    // The open fence must hold the region as pending plain text until the
    // closing fence arrives — never mount a half-built <pre><code>.
    const { container } = render(
      <AnswerBlocks content={"Sơ đồ dưới đây:\n\n```mermaid\n\ngraph TD\n  A --> B"} streaming />,
    );
    expect(container.querySelector("pre, code")).toBeNull();
    expect(container.textContent).toContain("Sơ đồ dưới đây:");
    expect(container.textContent).toContain("graph TD");
    err.mockRestore();
  });
  it("closed fence renders as a verbatim code block outside the markdown pipeline", () => {
    const { container } = render(
      <AnswerBlocks content={"Sơ đồ:\n\n```mermaid\n\ngraph TD\n  A --> B\n```"} streaming />,
    );
    const pre = container.querySelector("pre.rag-answer__code");
    expect(pre).not.toBeNull();
    // Inner source is verbatim: fence markers stripped, code text intact.
    expect(pre!.textContent).toBe("graph TD\n  A --> B");
    // No strong/price mangling inside code source.
    expect(container.querySelector("strong")).toBeNull();
  });
  it("streaming=false path renders closed fences through the same code block", () => {
    const { container } = render(<AnswerBlocks content={"```sql\nSELECT * FROM du_an;\n```"} />);
    const pre = container.querySelector("pre.rag-answer__code");
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toBe("SELECT * FROM du_an;");
  });
});
