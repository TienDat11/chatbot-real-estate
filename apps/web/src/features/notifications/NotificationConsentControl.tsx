"use client";

// Status dot colors are semantic state indicators (granted / denied / default),
// not decoration: green = notifications live, red = blocked by the browser,
// grey = not enabled. Kept as a small token map so the three states stay in one
// place and the badge can never drift from the native permission it reflects.
const DOT_COLOR: Record<NotificationPermission, string> = {
  granted: "#16a34a",
  denied: "#dc2626",
  default: "#94a3b8",
};

export function NotificationConsentControl({ enabled, nativePermission, loading, hydrated = true, onChange }: { enabled: boolean; nativePermission: NotificationPermission; loading: boolean; hydrated?: boolean; onChange: (enabled: boolean) => void }) {
  // Until the hook has read the real browser permission (hydrated), render a
  // stable neutral placeholder. The server always emits this state, so the
  // first client paint matches and there is no hydration mismatch on the label,
  // dot color, or aria attributes.
  const blocked = hydrated && nativePermission === "denied";
  const dotColor = blocked ? DOT_COLOR.denied : hydrated && enabled ? DOT_COLOR.granted : DOT_COLOR.default;
  const label = blocked ? "Đã chặn thông báo" : hydrated && enabled ? "Đã bật thông báo" : "Thông báo";
  const ariaLabel = blocked ? "Thông báo đã bị chặn" : hydrated && enabled ? "Tắt thông báo" : "Bật thông báo";
  return <button type="button" aria-pressed={hydrated && enabled} aria-label={ariaLabel} disabled={loading || blocked} onClick={() => onChange(!enabled)} style={{ display: "inline-flex", alignItems: "center", gap: 8, border: "1px solid #d9e2ec", borderRadius: 999, background: "white", padding: "6px 10px", cursor: loading ? "wait" : blocked ? "not-allowed" : "pointer", opacity: blocked ? 0.7 : 1 }}>
    <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: "50%", background: dotColor, display: "inline-block", flexShrink: 0 }} />
    <span>{label}</span>
  </button>;
}
