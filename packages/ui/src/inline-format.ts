/**
 * Inline + block formatting helpers for AnswerBlocks (Story 5.6).
 * Pure functions — unit-testable without a DOM.
 */

/**
 * BoldPrice regex: wraps VND amounts like "2,1 tỷ", "500 triệu", "1.2 tỷ/m²".
 * [RV-18/08] The "tr" shorthand must NOT match "5 trường học" — a negative
 * lookahead guards against a following Vietnamese letter.
 */
export const BOLD_PRICE_RE =
  /(\d{1,3}(?:[.,]\d{1,3})*\s*(?:tỷ|triệu|trieu|tr(?=[^a-zà-ỹA-ZÀ-Ỹ]|$))(?:\s*\/\s*m²)?)/i;

export function boldPrice(text: string): string {
  return text.replace(BOLD_PRICE_RE, (match) => `**${match}**`);
}

/** A markdown thematic-break line: 3+ of -, * or _ with optional spaces. */
export const THEMATIC_BREAK_LINE_RE = /^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/;

/** Case-insensitive <br> tag variants: <br>, <br/>, <br />. */
const BR_TAG_RE = /<br\s*\/?>/gi;

/**
 * [B2] Convert literal <br> tags the model emits into real line breaks.
 * ReactMarkdown v9 without rehype-raw renders unknown HTML as inert raw
 * text, so a `<br>` leaks visibly into the answer. Table lines (starting
 * with `|`) must stay ONE line or the GFM row structure breaks, so inside
 * them the break becomes a middle-dot bullet separator instead of `\n`.
 * Pure string preprocessing — no HTML is injected, XSS surface unchanged.
 */
export function normalizeBrTags(text: string): string {
  return text
    .split("\n")
    .map((line) => {
      if (!line.trimStart().startsWith("|")) return line.replace(BR_TAG_RE, "\n");
      // Cells commonly hold bullet lists ("• A<br>• B"); the separator must
      // not double up next to an existing bullet, so runs collapse to one.
      return line.replace(BR_TAG_RE, " • ").replace(/(?:\s*•\s*){2,}/g, " • ");
    })
    .join("\n");
}

/** [B1] A block consisting only of thematic-break line(s) is a divider. */
export function isDividerBlock(block: string): boolean {
  const lines = block.trim().split("\n");
  return lines.length > 0 && lines.every((l) => THEMATIC_BREAK_LINE_RE.test(l));
}

export const DISCLOSURE_KEYWORDS = [
  "lưu ý",
  "định hướng",
  "tham khảo",
  "chính thức từ sales",
  "khai báo",
];

export function isTableBlock(block: string): boolean {
  const lines = block.trim().split("\n");
  return lines.length >= 2 && tableRunEnd(lines, 0) !== null;
}

export function parseTable(block: string): { header: string[]; rows: string[][] } | null {
  if (!isTableBlock(block)) return null;
  const lines = block.trim().split("\n").filter((l) => l.includes("|"));
  const header = pipeCells(lines[0]);
  const rows: string[][] = [];
  for (let i = 1; i < lines.length; i++) {
    const row = pipeCells(lines[i]);
    if (row.every((cell) => SEPARATOR_CELL_RE.test(cell))) continue;
    rows.push(row);
  }
  return { header, rows };
}

export function splitBlocks(content: string): string[] {
  // [B2] Normalize literal <br> tags BEFORE any splitting: ReactMarkdown v9
  // without rehype-raw renders unknown HTML as inert raw text, so a model's
  // `<br>` would leak into the visible answer. Doing it here covers both the
  // legacy path and the streaming path (partitionStreamedBlocks routes
  // through splitBlocks, so the pending tail is normalized too).
  return normalizeBrTags(content)
    .split(/\n\s*\n/)
    .filter((b) => b.trim().length > 0)
    .flatMap(splitTableBoundaries);
}

/**
 * Splits one blank-line-delimited block so every GFM table lands in its own
 * block. LLM answers often emit an intro sentence and a table in the same
 * "paragraph" (no blank line between them); rendering that mixed block as a
 * paragraph puts a <table> inside <p>, which breaks validateDOMNesting and
 * hydrates differently on the client vs SSR. Pure and deterministic.
 */
export function splitTableBoundaries(block: string): string[] {
  const lines = block.trim().split("\n");
  const parts: string[] = [];
  let prose: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const tableEnd = tableRunEnd(lines, i);
    if (tableEnd !== null) {
      if (prose.length > 0) {
        parts.push(prose.join("\n"));
        prose = [];
      }
      parts.push(lines.slice(i, tableEnd).join("\n"));
      i = tableEnd;
    } else {
      prose.push(lines[i]);
      i += 1;
    }
  }
  if (prose.length > 0) parts.push(prose.join("\n"));
  return parts.filter((p) => p.trim().length > 0);
}

const SEPARATOR_CELL_RE = /^:?-+:?$/;

/** Splits a table line into trimmed cells, tolerating missing edge pipes. */
function pipeCells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());
}

function isSeparatorRow(line: string): boolean {
  const cells = pipeCells(line);
  return cells.length > 0 && cells.every((c) => SEPARATOR_CELL_RE.test(c));
}

/**
 * End index (exclusive) of a GFM table starting at `start`, or null.
 *
 * A GFM table opens on a header row containing a pipe — piped style
 * (`| A | B |`) or delimiter style (`A | B`) — followed by a delimiter row
 * declaring the same column count. Trailing pipes are never required: SSE
 * chunks cut mid-line, so an in-flight tail (`| 2PN | 3,2 tỷ` with the last
 * `|` still unflushed) is still part of the run and must not fall back to
 * "paragraph".
 */
function tableRunEnd(lines: string[], start: number): number | null {
  const header = lines[start] ?? "";
  const sep = lines[start + 1];
  if (!header.includes("|")) return null;
  if (sep === undefined || !isSeparatorRow(sep)) return null;
  // GFM requires the delimiter row to declare the same column count; this
  // also keeps prose like "x | y" over a horizontal-rule-looking line from
  // being mistaken for a table.
  if (pipeCells(sep).length !== pipeCells(header).length) return null;
  let end = start + 2; // header + delimiter consumed
  while (end < lines.length && lines[end].includes("|")) end++;
  return end;
}

/** True when the block embeds a GFM table among non-table prose lines. */
export function hasEmbeddedTable(block: string): boolean {
  const lines = block.trim().split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (tableRunEnd(lines, i) !== null && !(i === 0 && tableRunEnd(lines, i) === lines.length)) {
      return true;
    }
  }
  return false;
}

export function isHeadingBlock(block: string): boolean {
  return /^#{1,3}\s/.test(block.trim());
}

export function isListBlock(block: string): boolean {
  return /^\s*(?:[-*]|\d+\.)\s+/m.test(block.trim());
}

export type BlockKind = "table" | "heading" | "divider" | "list" | "callout" | "code" | "paragraph";

export function classifyBlock(block: string): BlockKind {
  const trimmed = block.trim();
  // Fenced code beats every other shape: fenced content legally contains
  // `#`/`|`/`-` lines that would otherwise misclassify as heading/table/list.
  // A closed fence = first AND last line are fence markers.
  const lines = trimmed.split("\n");
  if (lines.length > 1 && FENCE_LINE_RE.test(lines[0]) && FENCE_LINE_RE.test(lines[lines.length - 1])) {
    return "code";
  }
  if (isTableBlock(trimmed)) return "table";
  // Defensive: prose mixed with GFM table lines must never fall through to
  // "paragraph" — a <table> inside <p> fails validateDOMNesting and hydration.
  if (hasEmbeddedTable(trimmed)) return "table";
  // [B1] A pure thematic-break line (`---`/`***`/`___`) is a divider block:
  // rendered as a styled hairline, never as markdown <hr> inside a <p>.
  if (isDividerBlock(trimmed)) return "divider";
  if (isHeadingBlock(trimmed)) return "heading";
  // Lists beat disclosure keywords: keyword bullets must render as a real
  // list, not a callout paragraph whose <p> cannot legally contain <ul>.
  if (isListBlock(trimmed)) return "list";
  if (DISCLOSURE_KEYWORDS.some((kw) => trimmed.toLowerCase().includes(kw))) return "callout";
  return "paragraph";
}

const LIST_ITEM_LINE_RE = /^\s*(?:[-*+]|\d+\.)\s+/;
const HEADING_LINE_RE = /^\s*#{1,6}\s+/;
const FENCE_LINE_RE = /^\s*(?:```|~~~)/;
const HTML_LINE_RE = /^\s*</;

/**
 * Streaming-safety pass (premium chat): while `message.streaming` is true the
 * answer grows one token at a time, so the trailing block is almost always an
 * INCOMPLETE markdown construct (unclosed `##`, a table row missing its
 * delimiter, a half-written `- ` bullet, an unclosed `**bold`). Rendering that
 * tail through the normal markdown pipeline leaks the raw symbols to the
 * customer. `partitionStreamedBlocks` returns only blocks whose terminating
 * markup has arrived (kept as `stable`) plus the in-progress tail (kept as
 * `pending`) so the caller can render the tail as styled plain text.
 * Pure and deterministic — unit-testable without a DOM.
 */
export function partitionStreamedBlocks(content: string): {
  stable: string[];
  pending: string | null;
} {
  // Fast path: a stream that already looks complete renders exactly as the
  // non-streaming path does (splitBlocks re-applies the same blank-line split).
  const blocks = splitBlocks(content);
  if (blocks.length === 0) return { stable: [], pending: null };

  const stable: string[] = [];
  let pendingBlocks: string[] = [];
  // Fence state across blocks: splitBlocks splits on blank lines, so a fence
  // containing a blank line ("```mermaid\n\nchart...") arrives as SEVERAL
  // fragments. The check must run BEFORE the completeness test: a non-last
  // fragment that merely OPENS a fence would otherwise be promoted to stable
  // and mount a half-built <pre><code> mid-stream. Instead, once a fence
  // opens, every fragment from that block onward folds into pending until
  // the closing fence line arrives, then the folded region promotes as ONE
  // code block — identical shape to a fence that streamed within one block.
  let fenceOpen = false;
  for (let i = 0; i < blocks.length; i++) {
    if (fenceOpen) {
      pendingBlocks.push(blocks[i]);
      const lines = blocks[i].trim().split("\n");
      if (FENCE_LINE_RE.test(lines[lines.length - 1])) {
        // Closing fence landed: the whole folded region becomes one stable
        // code block; join with the same blank-line separator splitBlocks
        // removed so the fenced source stays verbatim.
        fenceOpen = false;
        stable.push(pendingBlocks.join("\n\n"));
        pendingBlocks = [];
      }
      continue;
    }
    const lines = blocks[i].trim().split("\n");
    if (FENCE_LINE_RE.test(lines[0])) {
      const closed = lines.length > 1 && FENCE_LINE_RE.test(lines[lines.length - 1]);
      if (closed) {
        // Self-contained fenced block (open and close in one fragment).
        stable.push(blocks[i]);
      } else {
        // Bare opening fence: fold it and everything after into pending.
        fenceOpen = true;
        pendingBlocks.push(blocks[i]);
      }
      continue;
    }
    const isLast = i === blocks.length - 1;
    // Only the last block can still be growing; earlier blocks were closed by
    // the blank line that separated them. Self-terminating shapes (complete
    // table runs, thematic breaks) are stable even as the final block.
    const complete = !isLast || isBlockTerminated(blocks[i]);
    if (complete) stable.push(blocks[i]);
    else return { stable, pending: blocks[i] };
  }
  if (fenceOpen) return { stable, pending: pendingBlocks.join("\n\n") };
  // A trailing `--`/`-` fragment is one keystroke from becoming a thematic
  // break but is not one yet: hold it back as pending plain text so the
  // divider never flashes as literal dashes ([B1]).
  const last = stable[stable.length - 1];
  if (last !== undefined && isPartialDividerTail(last)) {
    return { stable: stable.slice(0, -1), pending: last };
  }
  return { stable, pending: null };
}

/** Trailing fragment that may still grow into a thematic break (`--`, `- -`). */
function isPartialDividerTail(block: string): boolean {
  const text = block.trim();
  if (!/^-[\s-]*$/.test(text)) return false;
  return !THEMATIC_BREAK_LINE_RE.test(text);
}

/**
 * Heuristic: has this block received enough markup to be rendered as markdown
 * without leaking raw delimiters? Conservative by design — a block that looks
 * unfinished falls back to plain text for one frame at worst.
 */
function isBlockTerminated(block: string): boolean {
  const text = block.trim();
  if (text.length === 0) return true;
  const lines = text.split("\n");

  // [B1] Thematic break: a complete `---` line is already a stable divider;
  // a partial tail (`--`, `- -`) has not decided its shape yet and stays
  // pending plain text.
  if (lines.some((l) => THEMATIC_BREAK_LINE_RE.test(l))) return true;

  // Heading: needs a `#` marker AND at least one non-empty body line after it,
  // otherwise "## Giá căn" alone would flash a heading with no content.
  if (lines.some((l) => HEADING_LINE_RE.test(l))) {
    const firstContent = lines.findIndex((l) => HEADING_LINE_RE.test(l));
    return lines.slice(firstContent + 1).some((l) => l.trim().length > 0);
  }

  // List: an in-progress list item ("- " with no content yet, or a lone item
  // whose next line may still convert the shape) stays plain text until a
  // non-list line or blank line terminates it.
  if (LIST_ITEM_LINE_RE.test(text)) return false;

  // Table: the run must include a header, a delimiter row AND at least one
  // data row after the delimiter (a bare header+delimiter pair renders an
  // empty table and then reflows once rows land).
  const tableEnd = tableRunEnd(lines, 0);
  if (tableEnd !== null) return tableEnd - 0 > 2;
  // Any pipe not yet part of a completed table run is an in-flight GFM row:
  // remark-gfm renders it as literal "|" text until the delimiter row lands.
  if (lines.some((l) => l.includes("|"))) return false;

  // Fenced code: needs the closing fence.
  if (FENCE_LINE_RE.test(lines[0] ?? "")) {
    return lines.length > 1 && FENCE_LINE_RE.test(lines[lines.length - 1]);
  }

  // Prose: an unclosed **bold / *italic / `code` span mid-sentence still
  // leaks; hold the tail back until the delimiter count balances.
  const stars = (text.match(/\*\*/g) ?? []).length;
  if (stars % 2 !== 0) return false;
  const backticks = (text.match(/`/g) ?? []).length;
  if (backticks % 2 !== 0) return false;
  return true;
}

const HEADING_MARK_RE = /^\s*#{1,6}\s+/gm;
const LIST_MARK_RE = /^\s*(?:[-*+]|\d+\.)\s+/gm;
const DELIMITER_ROW_RE = /^\s*\|?[\s:|-]+\|?\s*$/;

/**
 * Clean the in-progress tail for plain-text rendering: markdown marker
 * characters (#, list dashes, table pipes, stray emphasis/backticks) are
 * stripped so the customer never sees raw delimiters, while the wording
 * itself stays visible one frame before its block completes.
 */
export function stripMarkdownMarkers(text: string): string {
  const withoutHeadings = text.replace(HEADING_MARK_RE, "");
  const withoutLists = withoutHeadings.replace(LIST_MARK_RE, "");
  const lines = withoutLists.split("\n").filter((line) => {
    const trimmed = line.trim();
    // Drop table delimiter rows ("|---|---|") outright.
    if (trimmed.includes("-") && DELIMITER_ROW_RE.test(trimmed)) return false;
    // [B1] Drop thematic-break lines ("---") from the pending tail; a
    // complete one promotes to a divider block on the next render pass.
    return !THEMATIC_BREAK_LINE_RE.test(trimmed);
  });
  return lines
    .map((line) => line.replace(/\|/g, " ").replace(/[*`_]+/g, "").trimEnd())
    .join("\n")
    .trim();
}

/**
 * True when markdown confined to this block can only ever produce inline
 * elements, making a <p> wrapper legal. Any block that could make
 * ReactMarkdown emit <ul>/<ol>/<table>/<pre>/<h*> inside ParagraphBlock's
 * <p> must take the neutral non-<p> container instead.
 */
export function canRenderAsP(block: string): boolean {
  if (hasEmbeddedTable(block)) return false;
  const lines = block.trim().split("\n");
  return !lines.some(
    (l) =>
      LIST_ITEM_LINE_RE.test(l) ||
      HEADING_LINE_RE.test(l) ||
      FENCE_LINE_RE.test(l) ||
      THEMATIC_BREAK_LINE_RE.test(l) ||
      HTML_LINE_RE.test(l),
  );
}