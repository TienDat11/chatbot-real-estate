/**
 * Transport-agnostic subscription primitives shared by every realtime port.
 *
 * PURE TypeScript: no firebase imports, no infrastructure imports. These types
 * are the vocabulary that keeps the domain and application layers decoupled
 * from whichever realtime transport happens to be wired in the composition
 * root (Firestore today, WebSocket/Socket.IO later).
 */

/** Opaque handle returned by every subscribe call; call `unsubscribe()` on unmount. */
export interface RealtimeSubscriptionHandle {
  unsubscribe(): void;
}

/**
 * Coarse lifecycle of the underlying transport connection. Adapters map their
 * own signals onto these three states:
 *   connecting — subscription requested, no data yet;
 *   active     — data is flowing (first snapshot / socket open + subscribed);
 *   error      — the transport failed; `RealtimeChannelError` carries detail.
 */
export type RealtimeConnectionState = "connecting" | "active" | "error";

/**
 * Failure surfaced by a realtime adapter. Carries the raw cause when the
 * underlying transport exposes one so the UI can show actionable messages.
 */
export class RealtimeChannelError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "RealtimeChannelError";
  }
}
