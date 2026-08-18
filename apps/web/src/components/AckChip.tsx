"use client";

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
        background: "#EAF2FF",
        border: "1px solid #D7E4FF",
        color: "#1F46A8",
        borderRadius: 999,
        padding: "6px 12px",
        fontSize: 13,
        fontWeight: 600,
        minHeight: 32,
      }}
    >
      <span aria-hidden="true" style={{ display: "inline-flex", gap: 3 }}>
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: "#1F46A8" }} />
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: "#1F46A8" }} />
        <span style={{ width: 5, height: 5, borderRadius: "50%", background: "#1F46A8" }} />
      </span>
      AI đã nhận câu hỏi
    </div>
  );
}
