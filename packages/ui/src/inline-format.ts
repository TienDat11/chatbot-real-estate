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

export const DISCLOSURE_KEYWORDS = [
  "lưu ý",
  "định hướng",
  "tham khảo",
  "chính thức từ sales",
  "khai báo",
];

export function isTableBlock(block: string): boolean {
  const trimmed = block.trim().split("\n");
  return trimmed.length >= 2 && trimmed[0].trim().startsWith("|") && trimmed[trimmed.length - 1].trim().endsWith("|");
}

export function parseTable(block: string): { header: string[]; rows: string[][] } | null {
  if (!isTableBlock(block)) return null;
  const lines = block.trim().split("\n").filter((l) => l.trim().startsWith("|"));
  const cells = (line: string) =>
    line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((x) => x.trim());
  const header = cells(lines[0]);
  const rows: string[][] = [];
  for (let i = 1; i < lines.length; i++) {
    const row = cells(lines[i]);
    if (row.every((cell) => /^:?-+:?$/.test(cell))) continue;
    rows.push(row);
  }
  return { header, rows };
}

export function splitBlocks(content: string): string[] {
  return content.split(/\n\s*\n/).filter((b) => b.trim().length > 0);
}

export function isHeadingBlock(block: string): boolean {
  return /^#{1,3}\s/.test(block.trim());
}

export function isListBlock(block: string): boolean {
  return /^\s*(?:[-*]|\d+\.)\s+/m.test(block.trim());
}

export type BlockKind = "table" | "heading" | "list" | "callout" | "paragraph";

export function classifyBlock(block: string): BlockKind {
  const trimmed = block.trim();
  if (isTableBlock(trimmed)) return "table";
  if (isHeadingBlock(trimmed)) return "heading";
  if (DISCLOSURE_KEYWORDS.some((kw) => trimmed.toLowerCase().includes(kw))) return "callout";
  if (isListBlock(trimmed)) return "list";
  return "paragraph";
}