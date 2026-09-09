import { describe, expect, it } from "vitest";
import { normalizeMath, normalizeMathBody, splitCodeSegments } from "./math-format";

describe("normalizeMath — reported production bug (Vietnamese loan example)", () => {
  const reported = [
    "Thời gian trả nợ gốc còn lại: $120 - 18 = 102$ tháng.",
    "Tiền gốc trả hàng tháng: $4.000.000.000 \\text{ đồng} / 102 \\approx \\mathbf{39,22 \\text{ triệu đồng/tháng}}$.",
    "Tiền lãi tháng đầu tiên sau ưu đãi (tháng 19): $4.000.000.000 \\times (10% / 12) \\approx \\mathbf{33,33 \\text{ triệu đồng/tháng}}$.",
  ].join("\n");

  it("removes every raw LaTeX delimiter and command", () => {
    const out = normalizeMath(reported);
    expect(out).not.toContain("$");
    expect(out).not.toContain("\\text");
    expect(out).not.toContain("\\mathbf");
    expect(out).not.toContain("\\approx");
    expect(out).not.toContain("\\times");
  });

  it("renders the math as readable Unicode prose", () => {
    const out = normalizeMath(reported);
    expect(out).toContain("120 - 18 = 102 tháng");
    expect(out).toContain("4.000.000.000 đồng / 102 ≈ 39,22 triệu đồng/tháng");
    expect(out).toContain("4.000.000.000 × (10% / 12) ≈ 33,33 triệu đồng/tháng");
  });

  it("keeps Vietnamese text inside nested \\mathbf{… \\text{…} …} intact", () => {
    const out = normalizeMath("$\\mathbf{39,22 \\text{ triệu đồng/tháng}}$");
    expect(out).toBe("39,22 triệu đồng/tháng");
  });
});

describe("normalizeMath — delimiters and commands", () => {
  it("handles display math $$…$$ across lines", () => {
    const out = normalizeMath("$$\n\\frac{A}{B} = C\n$$");
    expect(out).toBe("(A)/(B) = C");
  });

  it("handles \\( … \\) and \\[ … \\]", () => {
    expect(normalizeMath("\\(x^2 + y^2\\)")).toBe("x² + y²");
    expect(normalizeMath("\\[a \\le b\\]")).toBe("a ≤ b");
  });

  it("converts common symbols and scripts", () => {
    expect(normalizeMath("$a \\times b \\div c \\approx d$")).toBe("a × b ÷ c ≈ d");
    expect(normalizeMath("$10^{10}$ và $H_2O$")).toBe("10¹⁰ và H₂O");
    expect(normalizeMath("$x \\ne y \\ge 5$")).toBe("x ≠ y ≥ 5");
  });

  it("unwraps \\text{ } and preserves escaped \\%", () => {
    expect(normalizeMath("$10\\% / 12$")).toBe("10% / 12");
  });

  it("leaves genuine currency amounts alone (no math signal)", () => {
    expect(normalizeMath("Giá $500 và $1000")).toBe("Giá $500 và $1000");
  });

  it("keeps inline code verbatim", () => {
    expect(normalizeMath("dùng `$x \\approx 1$` trong code")).toBe("dùng `$x \\approx 1$` trong code");
  });

  it("[streaming-safe] unclosed $ renders as plain text until closed", () => {
    expect(normalizeMath("công thức $120 - 18 =")).toBe("công thức $120 - 18 =");
  });
});

describe("normalizeMath — stray HTML react-markdown would drop", () => {
  it("converts <br> and &lt;br&gt; to newlines", () => {
    expect(normalizeMath("dòng 1<br>dòng 2")).toBe("dòng 1\ndòng 2");
    expect(normalizeMath("dòng 1&lt;br&gt;dòng 2")).toBe("dòng 1\ndòng 2");
  });

  it("maps <b>/<i> to markdown emphasis and strips layout tags", () => {
    expect(normalizeMath("<b>2 tỷ</b> <p>giá</p>")).toBe("**2 tỷ** giá");
    expect(normalizeMath("<i>tham khảo</i>")).toBe("*tham khảo*");
  });

  it("decodes common entities", () => {
    expect(normalizeMath("1&nbsp;triệu &amp; 2&nbsp;tỷ")).toBe("1 triệu & 2 tỷ");
  });
});

describe("splitCodeSegments", () => {
  it("marks fenced and inline code", () => {
    const segs = splitCodeSegments("a `b` c");
    expect(segs.filter((s) => s.code).map((s) => s.value)).toEqual(["`b`"]);
  });

  it("treats an unterminated fence as literal text", () => {
    const segs = splitCodeSegments("a ```python\ncode tail");
    expect(segs.every((s) => !s.code)).toBe(true);
  });
});

describe("normalizeMathBody", () => {
  it("collapses \\frac{1}{2} and \\sqrt{x}", () => {
    expect(normalizeMathBody("\\frac{1}{2}")).toBe("(1)/(2)");
    expect(normalizeMathBody("\\sqrt{16}")).toBe("√(16)");
  });
});
