"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  boldPrice,
  canRenderAsP,
  classifyBlock,
  parseTable,
  partitionStreamedBlocks,
  splitBlocks,
  stripMarkdownMarkers,
} from "./inline-format";
import { normalizeMath } from "./math-format";

// Colors mirror the app's premium proptech tokens (apps/web/src/lib/tokens.ts):
// navy #0E2A47 primary, warm border neutral #E9E2D6. The ui package stays
// dependency-free, so hex values are kept in sync by convention.

export interface AnswerBlocksProps {
  content: string;
  className?: string;
  /**
   * Streaming-safety switch (premium chat): while true, only COMPLETE markdown
   * blocks go through the normal pipeline; the in-progress tail renders as
   * styled plain text so unclosed `##`/`|`/`-`/`**` never leak as raw symbols.
   * When false the streaming partition is skipped and every block goes through
   * the same pipeline as before — structurally the legacy block flow, with the
   * intentional premium styling (callouts/cards/colors); only the streaming
   * partition above is new behavior.
   */
  streaming?: boolean;
}

export function AnswerBlocks({ content, className, streaming = false }: AnswerBlocksProps) {
  const { stable, pending } = streaming
    ? partitionStreamedBlocks(content)
    : { stable: splitBlocks(content), pending: null };
  return (
    <div
      className={cn("rag-answer", className)}
      style={{ fontSize: "var(--fs-body, 17px)", lineHeight: "var(--fs-body-line, 28px)" }}
      aria-live={streaming ? "polite" : undefined}
    >
      {stable.map((block, i) => {
        const kind = classifyBlock(block);
        return <BlockSwitch key={i} block={block} kind={kind} />;
      })}
      {/* In-progress tail: plain text only — no markdown pipeline can emit a
          half-built <table>/<h*> from markup that has not closed yet, and the
          marker strip keeps raw #/|/- glyphs out of the visible copy. Font
          stays at the body scale so the pending tail never flashes larger
          than the fixed bubble frame ([B3]). */}
      {pending !== null && (
        <div className="rag-answer__pending" style={{ margin: "8px 0", fontSize: "var(--fs-body, 17px)", lineHeight: "var(--fs-body-line, 28px)", color: "#1A2233", whiteSpace: "pre-wrap", maxWidth: "65ch" }}>
          {stripMarkdownMarkers(pending)}
        </div>
      )}
    </div>
  );
}

/** Minimal className joiner (this package has no clsx dependency). */
function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

const BlockSwitch = memo(function BlockSwitch({
  block,
  kind,
}: {
  block: string;
  kind: string;
}) {
  switch (kind) {
    case "table": return <TableBlock block={block} />;
    case "heading": return <HeadingBlock block={block} />;
    case "divider": return <DividerBlock />;
    case "callout": return <CalloutBlock block={block} />;
    case "list": return <ListBlock block={block} />;
    case "code": return <CodeBlock block={block} />;
    default: return <ParagraphBlock block={block} />;
  }
});

// Heading cap ("khung chữ cố định"): answer headings sit at most ~1.15x the
// body scale so a model-emitted `##` never blows up the bubble. Navy token
// ink, fixed line-height; size is one clamp, not a per-level ramp.
const HEADING_STYLE = {
  fontSize: "calc(var(--fs-body, 17px) * 1.15)",
  fontWeight: 600,
  color: "#0E2A47",
  margin: "14px 0 8px",
  lineHeight: 1.35,
  maxWidth: "65ch",
} as const;

  // [B3] Root cause of "entire answer bold-large": models emit a `##` heading
  // and the following paragraph WITHOUT a blank line, so splitBlocks hands
  // them to us as ONE block. Only the heading LINE itself may take heading
  // style; every later line falls back to the body-scale paragraph renderer,
  // otherwise a long answer inherits bold navy heading typography.
  const lines = block.split("\n");
  const headingLine = lines[0] ?? "";
  const bodyLines = lines.slice(1).filter((l) => l.trim().length > 0);
  return (
    <>
      <div className="rag-answer__heading" style={HEADING_STYLE}>
        {renderInline(text)}
      </div>
      {bodyLines.map((line, i) => (
        <ParagraphBlock key={i} block={line} />
      ))}
    </>
  );
}

function DividerBlock() {
  // Thematic break (`---`) as a real block element OUTSIDE any <p>: a hairline
  // in the muted border token. ReactMarkdown's <hr> would otherwise nest under
  // ParagraphBlock's <p> and fail validateDOMNesting during hydration ([B1]).
  return (
    <div
      className="rag-answer__divider"
      role="separator"
      aria-orientation="horizontal"
      style={{ margin: "12px 0", borderTop: "1px solid #E9E2D6" }}
    />
  );
}

function CodeBlock({ block }: { block: string }) {
  // Fenced code renders VERBATIM outside the markdown pipeline: renderInline
  // would treat source as prose (boldPrice mangling amounts, spans wrapping
  // glyphs) and a <pre> emitted inside ParagraphBlock's <p> breaks nesting.
  // Fence marker lines are stripped; the inner source is shown as-is.
  const lines = block.trim().split("\n");
  const source = lines.slice(1, -1).join("\n").replace(/^\n+/, "").replace(/\n+$/, "");
  return (
    <pre
      className="rag-answer__code"
      style={{
        margin: "10px 0",
        padding: 12,
        background: "#F7F4ED",
        border: "1px solid #E9E2D6",
        borderRadius: 8,
        overflowX: "auto",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        fontSize: 15,
        lineHeight: 1.5,
        color: "#1A2233",
      }}
    >
      <code style={{ whiteSpace: "pre" }}>{source}</code>
    </pre>
  );
}

function TableBlock({ block }: { block: string }) {
  const parsed = parseTable(block);
  if (!parsed) {
    // Parse failed for a block the classifier already marked as table-shaped
    // (e.g. a malformed in-flight SSE run). Defer the raw content in a neutral
    // non-<p> container so no markdown path can emit <table> inside <p>.
    return (
      <div style={{ margin: "10px 0", whiteSpace: "pre-wrap", fontSize: 16, color: "#1A2233" }}>
        {block}
      </div>
    );
  }
  const { header, rows } = parsed;
  const useCards = header.length > 3 || rows.length === 0;
  return (
    <div style={{ margin: "10px 0", overflowX: "auto" }}>
      {useCards ? <DefinitionCards header={header} rows={rows} /> : (
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 16 }}>
          <thead>
            <tr>
              {header.map((h) => <th key={h} style={{ textAlign: "left", fontSize: 15, fontWeight: 600, color: "#1A2233", padding: "8px 12px", borderBottom: "2px solid #E9E2D6" }}>{renderInline(h)}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri} style={{ background: "#FFFFFF" }}>
                {row.map((cell, ci) => <td key={ci} style={{ padding: "8px 12px", fontSize: 16, borderBottom: "1px solid #E9E2D6", fontVariantNumeric: "tabular-nums" }}>{renderInline(cell)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function DefinitionCards({ header, rows }: { header: string[]; rows: string[][] }) {
  if (rows.length === 0) {
    return <div style={{ color: "#5B6478", fontSize: 15 }}>{header.join(", ")}</div>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {rows.map((row, ri) => (
        <div key={ri} style={{ padding: "8px 0", borderBottom: "1px solid #E9E2D6" }}>
          {row.map((cell, ci) => {
            const label = header[ci] ?? "col-" + (ci + 1);
            const isNumeric = /[0-9.,]+/.test(cell);
            return (
              <div key={ci} style={{ display: "flex", justifyContent: "space-between", gap: 8, padding: "4px 0" }}>
                <span style={{ fontSize: 14, color: "#5B6478" }}>{label}</span>
                <span style={{ fontSize: isNumeric ? 17 : 15, fontWeight: isNumeric ? 600 : 400, color: "#1A2233", fontVariantNumeric: "tabular-nums" }}>{renderInline(cell)}</span>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function ListBlock({ block }: { block: string }) {
  const items = block.split("\n").map((l) => l.replace(/^\s*(?:[-*]|\d+\.)\s+/, "")).filter(Boolean);
  return (
    <ul style={{ listStyle: "none", margin: "8px 0", padding: 0 }}>
      {items.map((item, i) => (
        <li key={i} style={{ display: "flex", gap: 10, margin: "10px 0", alignItems: "flex-start" }}>
          <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: "50%", background: "#0E2A47", marginTop: 9, flexShrink: 0 }} />
          <span style={{ fontSize: "var(--fs-body, 17px)", color: "#1A2233" }}>{renderInline(item)}</span>
        </li>
      ))}
    </ul>
  );
}

function CalloutBlock({ block }: { block: string }) {
  // Disclosure copy ("Lưu ý", "Định hướng", ...) renders as a plain paragraph
  // on the normal surface — no yellow/amber callout box, no icon. The keyword
  // text itself keeps the disclosure semantics, the wrapper carries none.
  return <ParagraphBlock block={block} />;
}

const PARA_STYLE = {
  fontSize: "var(--fs-body, 17px)",
  lineHeight: "var(--fs-body-line, 28px)",
  color: "#1A2233",
  margin: "8px 0",
  maxWidth: "65ch",
};

function ParagraphBlock({ block }: { block: string }) {
  // Last line of defense: if residual markdown could still expand into a
  // block-level element (<ul>/<ol>/<table>/<pre>/<h*>/<hr>), swap the semantic
  // <p> for a neutral <div> instead of risking invalid nesting.
  if (!canRenderAsP(block)) return <div style={PARA_STYLE}>{renderInline(block)}</div>;
  return <p style={PARA_STYLE}>{renderInline(block)}</p>;
}

function renderInline(text: string) {
  // Normalize first (math → Unicode, stray HTML → markdown), THEN boldPrice,
  // so price-bolding sees clean digits, never a `$…$`/`\text{}` span.
  const processed = boldPrice(normalizeMath(text));
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }: { children?: React.ReactNode }) => children,
        strong: ({ children }: { children?: React.ReactNode }) => <strong style={{ color: "#0E2A47", fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{children}</strong>,
        // [B1] Defensive: a stray thematic break that somehow reaches an
        // inline render must never emit <hr> (a <p><hr></p> pairing fails
        // validateDOMNesting and hydrates differently); collapse to nothing.
        hr: () => null,
        // [B3] Defensive heading cap: markdown spans (`#` mid-paragraph via
        // setext/ATX reflow) must never render giant text inside the bubble.
        h1: InlineHeading, h2: InlineHeading, h3: InlineHeading,
        h4: InlineHeading, h5: InlineHeading, h6: InlineHeading,
      }}
    >
      {processed}
    </ReactMarkdown>
  );
}

const InlineHeading = ({ children }: { children?: React.ReactNode }) => (
  <span className="rag-answer__heading" style={{ fontWeight: 600, color: "#0E2A47" }}>
    {children}
  </span>
);