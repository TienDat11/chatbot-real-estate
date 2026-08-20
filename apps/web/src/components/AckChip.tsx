"use client";

import { C } from "@/lib/tokens";

/**
 * AckChip — zero-latency "AI đã nhận câu hỏi" feedback.
 * Shown in the assistant bubble as soon as the `ack` SSE event fires (< 100ms),
 * before any token streams.
 */

interface AckChipProps {
  /** Show the chip (received ack) — otherwise hidden. */
  visible: boolean;
}

export function AckChip({ visible }: AckChipProps) {
  if (!visible) return null;
  return (
    <div
      aria-live="polite"
      className="ack-chip"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        background: C.primarySoft,
        border: "1px solid " + C.primaryBorder,
        color: C.primary,
        borderRadius: 999,
        padding: "6px 12px",
        fontSize: 13,
        fontWeight: 600,
        minHeight: 32,
      }}
    >
      <span aria-hidden="true" style={{ display: "inline-flex", gap: 3 }}>
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: C.primary }} />
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: C.primary }} />
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: C.primary }} />
      </span>
      AI đã nhận câu hỏi
    </div>
  );
}
