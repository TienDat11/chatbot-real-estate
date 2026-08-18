"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  boldPrice,
  classifyBlock,
  parseTable,
  splitBlocks,
} from "./inline-format";

export interface AnswerBlocksProps {
  content: string;
  className?: string;
}

export function AnswerBlocks({ content, className }: AnswerBlocksProps) {
  const blocks = splitBlocks(content);
  return (
    <div className={className} style={{ fontSize: "var(--fs-body, 17px)", lineHeight: "var(--fs-body-line, 28px)" }}>
      {blocks.map((block, i) => {
        const isLast = i === blocks.length - 1;
        const kind = classifyBlock(block);
        return <BlockSwitch key={i} block={block} kind={kind} isLast={isLast} />;
      })}
    </div>
  );
}

const BlockSwitch = memo(function BlockSwitch({
  block,
  kind,
  isLast,
}: {
  block: string;
  kind: string;
  isLast: boolean;
}) {
  switch (kind) {
    case "table": return <TableBlock block={block} isLast={isLast} />;
    case "heading": return <HeadingBlock block={block} />;
    case "callout": return <CalloutBlock block={block} />;
    case "list": return <ListBlock block={block} />;
    default: return <ParagraphBlock block={block} />;
  }
});

function HeadingBlock({ block }: { block: string }) {
  const text = block.replace(/^#{1,3}\s+/, "");
  return (
    <div style={{ fontSize: 19, fontWeight: 600, color: "#1F46A8", margin: "14px 0 8px", lineHeight: "26px" }}>
      {renderInline(text)}
    </div>
  );
}

function TableBlock({ block, isLast }: { block: string; isLast: boolean }) {
  const parsed = parseTable(block);
  if (!parsed) return <ParagraphBlock block={block} />;
  const { header, rows } = parsed;
  const useCards = header.length > 3 || rows.length === 0;
  return (
    <div style={{ margin: "10px 0", overflowX: "auto", minHeight: isLast ? 88 : undefined }}>
      {useCards ? <DefinitionCards header={header} rows={rows} /> : (
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 16 }}>
          <thead>
            <tr>
              {header.map((h) => <th key={h} style={{ textAlign: "left", fontSize: 15, fontWeight: 600, color: "#1A2233", padding: "8px 12px", borderBottom: "2px solid #E9ECF2" }}>{renderInline(h)}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri} style={{ background: ri % 2 === 0 ? "#F7F8FA" : "#FFFFFF" }}>
                {row.map((cell, ci) => <td key={ci} style={{ padding: "8px 12px", fontSize: 16, borderBottom: "1px solid #E9ECF2", fontVariantNumeric: "tabular-nums" }}>{renderInline(cell)}</td>)}
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
        <div key={ri} style={{ background: "#F7F8FA", borderRadius: 12, padding: "12px 14px" }}>
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
          <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: "50%", background: "#1F46A8", marginTop: 9, flexShrink: 0 }} />
          <span style={{ fontSize: "var(--fs-body, 17px)", color: "#1A2233" }}>{renderInline(item)}</span>
        </li>
      ))}
    </ul>
  );
}

function CalloutBlock({ block }: { block: string }) {
  return (
    <div role="note" style={{
      background: "#FFF8E6",
      borderLeft: "3px solid #D97706",
      borderRadius: 8,
      padding: "10px 14px",
      margin: "10px 0",
      fontSize: 15,
      color: "#5B6478",
    }}>
      <span aria-hidden="true" style={{ marginRight: 6 }}>ⓘ</span>
      {renderInline(block)}
    </div>
  );
}

function ParagraphBlock({ block }: { block: string }) {
  return (
    <p style={{ fontSize: "var(--fs-body, 17px)", lineHeight: "var(--fs-body-line, 28px)", color: "#1A2233", margin: "8px 0", maxWidth: "65ch" }}>
      {renderInline(block)}
    </p>
  );
}

function renderInline(text: string) {
  // Preprocess with boldPrice so VND amounts get the navy <strong> treatment.
  const processed = boldPrice(text);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }: { children?: React.ReactNode }) => children,
        strong: ({ children }: { children?: React.ReactNode }) => <strong style={{ color: "#1F46A8", fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{children}</strong>,
      }}
    >
      {processed}
    </ReactMarkdown>
  );
}