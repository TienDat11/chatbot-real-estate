/**
 * LLM output normalization for the shared UI renderers (Story: fix raw LaTeX
 * leaking into production chat bubbles).
 *
 * The model sometimes answers financial questions with LaTeX math — inline
 * `$...$`, display `$$...$$`, `\(...\)`, `\[...\]` — and commands like
 * `\text{}`, `\mathbf{}`, `\approx`, `\times`, `\frac{}{}`. react-markdown v9
 * (no remark-math/rehype-katex) renders those verbatim, so the user sees the
 * raw delimiters and backslash commands.
 *
 * Rather than pull in KaTeX (new dependency + CSS/font wiring right before a
 * production push), we convert the math to readable Unicode + plain text. For
 * Vietnamese financial prose this looks *better* than KaTeX: `4.000.000.000
 * đồng / 102 ≈ 39,22 triệu đồng/tháng` instead of a serif equation.
 *
 * All functions are pure and streaming-safe: an unclosed delimiter yields no
 * span, so a partial answer renders as plain text until it completes. Code
 * fences / inline code are passed through untouched so `` `$x$` `` stays
 * literal.
 */

/** A run of text with `code: true` must be emitted verbatim (no transforms). */
interface Segment {
  code: boolean;
  value: string;
}

/**
 * Splits `text` into code vs. non-code segments. A code segment is a fenced
 * block (``` ``` ``` or `~~~`) or inline code (`` ` `` / ``` `` ```). An
 * unterminated run (mid-stream) is treated as literal text, never swallowed.
 *
 * Exported so `inline-format.boldPrice` shares the exact same code-protection
 * rule (single source of truth; no circular import — this module imports
 * nothing from the UI package).
 */
export function splitCodeSegments(text: string): Segment[] {
  const segments: Segment[] = [];
  let buffer = "";
  let i = 0;
  const n = text.length;

  const flush = () => {
    if (buffer) {
      segments.push({ code: false, value: buffer });
      buffer = "";
    }
  };

  while (i < n) {
    const ch = text[i];
    let run = 0;
    while (text[i + run] === ch) run++;
    // Only `` ` `` (inline code) or ~~~/``` (3+-char fences) open a code span;
    // 1–2 tildes are literal text — Vietnamese uses "~" for "approximately".
    if (ch === "`" || (ch === "~" && run >= 3)) {
      const marker = ch;
      const tick = marker.repeat(run);
      const isFence = run >= 3;
      const searchFrom = i + run;
      let close = -1;
      if (isFence) {
        const re = new RegExp(`^\\s*${marker}{${run},}`, "m");
        const rest = text.slice(searchFrom);
        const m = re.exec(rest);
        if (m && m.index !== undefined) close = searchFrom + m.index;
      } else {
        close = text.indexOf(tick, searchFrom);
      }

      if (close !== -1) {
        flush();
        const end = isFence ? text.indexOf("\n", close) : close + run;
        const codeEnd = end === -1 || !isFence ? (isFence ? text.length : close + run) : end;
        segments.push({ code: true, value: text.slice(i, codeEnd) });
        i = codeEnd;
        continue;
      }
      // Unterminated (streaming tail): emit the ticks as literal text.
      buffer += tick;
      i += run;
      continue;
    }
    buffer += ch;
    i++;
  }
  flush();
  return segments;
}

/** Apply `fn` to every non-code segment; code segments pass through verbatim. */
export function mapOutsideCode(text: string, fn: (plain: string) => string): string {
  return splitCodeSegments(text)
    .map((seg) => (seg.code ? seg.value : fn(seg.value)))
    .join("");
}

/** Unicode replacements for the LaTeX commands the model actually emits. */
const SYMBOLS: Record<string, string> = {
  approx: "≈",
  cong: "≅",
  equiv: "≡",
  times: "×",
  div: "÷",
  cdot: "·",
  ast: "*",
  star: "★",
  pm: "±",
  mp: "∓",
  le: "≤",
  leq: "≤",
  ge: "≥",
  geq: "≥",
  ne: "≠",
  neq: "≠",
  sim: "~",
  simeq: "≃",
  propto: "∝",
  infty: "∞",
  partial: "∂",
  nabla: "∇",
  to: "→",
  rightarrow: "→",
  Leftarrow: "←",
  leftarrow: "←",
  Rightarrow: "⇒",
  Rrightarrow: "⇒",
  Lrightarrow: "⇐",
  mapsto: "↦",
  in: "∈",
  notin: "∉",
  subset: "⊂",
  subseteq: "⊆",
  cup: "∪",
  cap: "∩",
  alpha: "α",
  beta: "β",
  gamma: "γ",
  delta: "δ",
  epsilon: "ε",
  varepsilon: "ε",
  zeta: "ζ",
  eta: "η",
  theta: "θ",
  iota: "ι",
  kappa: "κ",
  mu: "μ",
  nu: "ν",
  xi: "ξ",
  pi: "π",
  rho: "ρ",
  sigma: "σ",
  tau: "τ",
  upsilon: "υ",
  phi: "φ",
  varphi: "φ",
  chi: "χ",
  psi: "ψ",
  omega: "ω",
  Gamma: "Γ",
  Delta: "Δ",
  Theta: "Θ",
  Lambda: "Λ",
  Xi: "Ξ",
  Pi: "Π",
  Sigma: "Σ",
  Phi: "Φ",
  Psi: "Ψ",
  Omega: "Ω",
  percent: "%",
  degree: "°",
  circ: "°",
  ldots: "…",
  cdots: "…",
  dots: "…",
  quad: "  ",
  qquad: "    ",
};

const SUPERSCRIPTS: Record<string, string> = {
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
  "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
  "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
  n: "ⁿ", i: "ⁱ",
};

const SUBSCRIPTS: Record<string, string> = {
  "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
  "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
  "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
};

/** Convert a single math payload (delimiters already stripped) to plain text. */
export function normalizeMathBody(body: string): string {
  let s = body;

  // \left / \right sizing — drop the command, keep the delimiter char.
  s = s.replace(/\\(?:left|right|big|Big|bigg|Bigg)[lrm]?/g, "");

  // Text-family wrappers: \text{X}, \mathrm{X}, \mathbf{X}, \textbf{X}, …
  // Loop because these nest (\mathbf{… \text{…} …}); the inner unwrap must
  // finish before the outer one's `[^{}]*` can match.
  const wrapper =
    /\\(?:text|textrm|textsf|texttt|textbf|textit|emph|mathrm|mathbf|mathit|mathsf|mathtt|operatorname|boldsymbol|mathsf)\s*\{([^{}]*)\}/g;
  for (let pass = 0; pass < 5; pass++) {
    const next = s.replace(wrapper, "$1");
    if (next === s) break;
    s = next;
  }

  // \frac{A}{B} → (A)/(B); \frac12 → 1/2.
  s = s.replace(/\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)");
  s = s.replace(/\\d?frac\s*(\d)\s*(\d)/g, "$1/$2");
  s = s.replace(/\\sqrt\s*\{([^{}]*)\}/g, "√($1)");
  s = s.replace(/\\sqrt\s*(\w)/g, "√$1");

  // Escaped punctuation: \% \$ \& \# \_ \{ \} \\
  s = s.replace(/\\([%$&#_{}\\|])/g, "$1");

  // Named symbols (\approx, \times, \le, \alpha, …).
  s = s.replace(/\\([a-zA-Z]+)/g, (m, cmd: string) => SYMBOLS[cmd] ?? m);

  // Spacing commands: \, \; \: \! \ (backslash-space)
  s = s.replace(/\\[,;:! ]/g, " ");

  // Superscripts / subscripts: ^{2} → ², _{10} → ₁₀, x^2 → x².
  s = s.replace(/\^\{([^{}]*)\}/g, (_m, g: string) => mapScript(g, SUPERSCRIPTS, "^"));
  s = s.replace(/_\{([^{}]*)\}/g, (_m, g: string) => mapScript(g, SUBSCRIPTS, "_"));
  s = s.replace(/\^([0-9+\-=()n])/g, (_m, g: string) => SUPERSCRIPTS[g] ?? `^${g}`);
  s = s.replace(/_([0-9+\-=()])/g, (_m, g: string) => SUBSCRIPTS[g] ?? `_${g}`);

  // Drop any leftover LaTeX braces and stray backslash-commands we didn't map.
  s = s.replace(/[{}]/g, "");

  // Tidy whitespace (keep newlines for display math).
  s = s.replace(/[ \t]{2,}/g, " ");
  return s.trim();
}

function mapScript(group: string, table: Record<string, string>, fallbackPrefix: string): string {
  if (!group) return "";
  let mapped = "";
  let allMapped = true;
  for (const ch of group) {
    if (ch in table) mapped += table[ch];
    else {
      allMapped = false;
      break;
    }
  }
  return allMapped ? mapped : `${fallbackPrefix}${group}`;
}

const DOLLAR_SENTINEL = "\u0000DOLLAR\u0000";

/** Heuristic: does a `$…$` body look like math (vs. a plain currency amount)? */
function looksLikeMath(body: string): boolean {
  return (
    /\\/.test(body) ||
    /[=^_]/.test(body) ||
    /[×÷≈≤≥≠∞∑∫∂]/.test(body)
  );
}

/** Normalize math + stray HTML in one non-code segment. */
function normalizePlainSegment(seg: string): string {
  let s = seg;

  // Protect escaped \$ so it isn't read as a math delimiter.
  s = s.replace(/\\\$/g, DOLLAR_SENTINEL);

  // Display math first ($$…$$, \[…\]) so the inline pass can't eat one `$`.
  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_m, b: string) => normalizeMathBody(b));
  s = s.replace(/\\\[([\s\S]+?)\\\]/g, (_m, b: string) => normalizeMathBody(b));
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, (_m, b: string) => normalizeMathBody(b));
  // Inline $…$ — single line, non-empty. Only treat as math when the body
  // carries a math signal, so genuine currency like "$500 và $1000" survives.
  s = s.replace(/\$([^\n$]+?)\$/g, (_m, b: string) => (looksLikeMath(b) ? normalizeMathBody(b) : _m));

  // Stray HTML the model emits that react-markdown (no rehype-raw) would drop.
  s = s.replace(/(?:<br\s*\/?>|&lt;br\s*\/?&gt;)/gi, "\n");
  s = s.replace(/(?:<hr\s*\/?>|&lt;hr\s*\/?&gt;)/gi, "\n");
  s = s.replace(/<\/?(?:p|div|span|font|center|section|article|u)\b[^>]*>/gi, "");
  s = s.replace(/<(?:b|strong)\b[^>]*>([\s\S]*?)<\/(?:b|strong)>/gi, "**$1**");
  s = s.replace(/<(?:i|em)\b[^>]*>([\s\S]*?)<\/(?:i|em)>/gi, "*$1*");
  s = s.replace(/&nbsp;/gi, " ");
  s = s.replace(/&amp;/gi, "&");
  s = s.replace(/&quot;/gi, '"');
  s = s.replace(/&#0?39;|&apos;/gi, "'");
  s = s.replace(/&lt;/gi, "<");
  s = s.replace(/&gt;/gi, ">");

  // Restore escaped dollars as literal $.
  s = s.split(DOLLAR_SENTINEL).join("$");

  // Collapse runs of spaces introduced by unwrapping, and trailing spaces.
  s = s.replace(/[ \t]{2,}/g, " ");
  s = s.replace(/ +\n/g, "\n");
  return s;
}

/**
 * Full normalization pass for one block of LLM text: math → Unicode/plain,
 * stray HTML → markdown/newlines, code regions untouched. Run this BEFORE
 * `boldPrice` so price-bolding sees clean digits, not `$…$`/`\text{}`.
 */
export function normalizeMath(text: string): string {
  if (!/[$\\<&]/.test(text)) return text;
  return mapOutsideCode(text, normalizePlainSegment);
}
