/**
 * Anonymous-quota contract parsing (secure-wave spec §5).
 *
 * The server is the only quota authority; this module is a thin, total parser
 * over every wire shape that can carry a quota snapshot:
 *  - /query success payload (`quota` object, §5.1/§5.4)
 *  - HTTP 429 rejection body (`{ok:false, error:{code,message,quota}}`, §5.2)
 *  - SSE `error` frame (`{code, quota, lead_cta}`, §5.3)
 *
 * Every function is pure and returns null on malformed input instead of
 * throwing, so a bad payload degrades to "keep the previous UI state" rather
 * than breaking the chat. Pure over plain values (no Storage/DOM), so vitest
 * covers the whole contract in node.
 */

/** Client-side view of one server-authoritative quota snapshot. */
export interface QuotaState {
  /** Turns already consumed by this identity, or null for unlimited admin. */
  usedTurns: number | null;
  /** Turns left before the wall; null only for the unlimited admin shape. */
  remainingTurns: number | null;
  /** Effective cap: anonymous 3 (+5 once), customer 5 (+5 once), sales 10, admin null. */
  cap: number | null;
  /** True for registered customer, sales, and admin principals. */
  isAuthenticated: boolean;
  /** Bonus turns already granted to this identity; null renders as 0. */
  bonusGranted: number;
}

export type QuotaPolicy = "anonymous" | "customer" | "sales" | "admin";

/** Maps the wire snapshot to the approved product policy without changing authority. */
export function quotaPolicy(state: QuotaState): QuotaPolicy {
  if (!state.isAuthenticated) return "anonymous";
  if (state.cap === null && state.remainingTurns === null) return "admin";
  return state.cap === 10 ? "sales" : "customer";
}

/** True when this snapshot has a finite allowance that is fully consumed. */
export function isQuotaExhausted(state: QuotaState | null): boolean {
  return state !== null && state.remainingTurns !== null && state.remainingTurns <= 0;
}

/** Only customer-facing identities may receive the one-time lead bonus CTA. */
export function shouldShowLeadBonus(state: QuotaState | null): boolean {
  return state === null || quotaPolicy(state) === "anonymous" || quotaPolicy(state) === "customer";
}

/** Stable role-aware copy for the header status chip. */
export function quotaBadgeLabel(state: QuotaState): string {
  const policy = quotaPolicy(state);
  if (policy === "admin") return "Không giới hạn";
  if (state.remainingTurns === null) return "Tư vấn miễn phí";
  if (state.remainingTurns > 0) return `Còn ${state.remainingTurns} lượt`;
  return policy === "anonymous" ? "Hết lượt miễn phí" : "Hết lượt tư vấn";
}

/** Structured quota rejection shared by the 429 body and the SSE error frame. */
export interface QuotaExceededInfo {
  code: string;
  message: string;
  quota: QuotaState | null;
}

/** Error code the backend emits on every exhausted-identity attempt (§5.2). */
export const QUOTA_EXCEEDED_CODE = "ANONYMOUS_QUOTA_EXCEEDED";

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function asNonNegativeInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/**
 * Normalizes a raw `quota` snapshot object into QuotaState. Nulls are legal
 * values (authenticated shape §5.4), so only used_turns is mandatory: a
 * snapshot without a valid non-negative integer used_turns is malformed and
 * rejected wholesale (caller keeps its previous state).
 */
export function normalizeQuota(raw: unknown): QuotaState | null {
  const rec = asRecord(raw);
  if (!rec) return null;
  const rawUsedTurns = rec.used_turns;
  const usedTurns = rawUsedTurns === null ? null : asNonNegativeInt(rawUsedTurns);
  if (usedTurns === null && rawUsedTurns !== null) return null;
  return {
    usedTurns,
    remainingTurns: asNonNegativeInt(rec.remaining_turns),
    cap: asNonNegativeInt(rec.cap),
    isAuthenticated: rec.is_authenticated === true,
    bonusGranted: asNonNegativeInt(rec.bonus_granted) ?? 0,
  };
}

/** Extracts the quota snapshot from a /query success payload (§5.1). */
export function quotaFromQueryResponse(payload: unknown): QuotaState | null {
  return normalizeQuota(asRecord(payload)?.quota);
}

/**
 * Parses a structured error envelope from either wire shape: the JSON 429
 * body nests it under `error` while the SSE error frame carries `code`
 * top-level — both are accepted here so callers need one parser.
 */
export function parseQuotaErrorEnvelope(body: unknown): QuotaExceededInfo | null {
  const rec = asRecord(body);
  if (!rec) return null;
  const errRec = asRecord(rec.error) ?? rec;
  const code = typeof errRec.code === "string" ? errRec.code : null;
  if (!code) return null;
  return {
    code,
    message: typeof errRec.message === "string" ? errRec.message : "",
    quota: normalizeQuota(errRec.quota),
  };
}

/** True when the parsed envelope is the anonymous-quota-exhausted rejection. */
export function isQuotaExceeded(info: QuotaExceededInfo | null): boolean {
  return info?.code === QUOTA_EXCEEDED_CODE;
}

/**
 * Whether the LeadForm must be forced open right now (US-4): only an
 * anonymous principal with a known finite cap that has been fully spent.
 * Authenticated users (null cap/remaining) and unknown states never gate.
 */
export function shouldForceLeadForm(state: QuotaState | null): boolean {
  return shouldShowLeadBonus(state) && isQuotaExhausted(state);
}

/**
 * Optimistic post-lead quota update (AC4): the lead response only carries
 * `quota_bonus_granted`, so the effective cap/remaining are bumped locally
 * until the next server ack/done overwrites them with authoritative values.
 */
export function applyLeadBonus(state: QuotaState | null, bonusTurns: number): QuotaState | null {
  if (state === null || bonusTurns <= 0) return state;
  return {
    ...state,
    cap: state.cap === null ? null : state.cap + bonusTurns,
    remainingTurns: state.remainingTurns === null ? null : state.remainingTurns + bonusTurns,
    bonusGranted: state.bonusGranted + bonusTurns,
  };
}
