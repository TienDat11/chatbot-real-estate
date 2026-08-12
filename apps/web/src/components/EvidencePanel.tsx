"use client";

import { useState } from "react";
import { Collapse, Typography } from "antd";
import { FactsTable, SourcesList } from "@rag-ragre/ui";
import type { FactEvidence, Source } from "@rag-ragre/contracts";

interface EvidencePanelProps {
  sources: Source[];
  facts: FactEvidence[];
  /** Changes force the panel to remount/scroll when a new message streams. */
  activeMessageId?: string;
}

/**
 * Left collapsible rail aggregating sources + facts of the message currently
 * being processed, so advisors can quickly verify citations.
 */
export function EvidencePanel({ sources, facts, activeMessageId }: EvidencePanelProps) {
  const [open, setOpen] = useState(true);

  if (!sources.length && !facts.length) return null;

  return (
    <aside
      style={{
        width: 320,
        flexShrink: 0,
        background: "#FFFFFF",
        border: "1px solid #E9ECF2",
        borderRadius: 14,
        padding: 12,
        height: "fit-content",
        maxHeight: "calc(100vh - 180px)",
        overflowY: "auto",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Typography.Text strong style={{ fontSize: 13, color: "#1A2233" }}>
          Dẫn chứng gần nhất
        </Typography.Text>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          style={{
            border: "none",
            background: "transparent",
            color: "#1F46A8",
            fontSize: 12,
            cursor: "pointer",
            padding: 0,
          }}
        >
          {open ? "Thu gọn" : "Mở rộng"}
        </button>
      </div>

      <div key={activeMessageId ?? "empty"} style={{ marginTop: 8 }}>
        <Collapse
          ghost
          defaultActiveKey={open ? ["sources", "facts"] : []}
          items={[
            {
              key: "sources",
              label: (
                <Typography.Text style={{ fontSize: 12, color: "#5B6478" }}>
                  Nguồn tài liệu ({sources.length})
                </Typography.Text>
              ),
              children: <SourcesList sources={sources} max={8} />,
            },
            {
              key: "facts",
              label: (
                <Typography.Text style={{ fontSize: 12, color: "#5B6478" }}>
                  Sự kiện pháp lý ({facts.length})
                </Typography.Text>
              ),
              children: <FactsTable facts={facts} variant="cards" />,
            },
          ]}
        />
      </div>
    </aside>
  );
}
