/**
 * Anonymous identity + persisted project choice (stories 10.1-FE, 10.3).
 *
 * Why a separate module instead of inline helpers in ChatPage: these functions
 * are pure over an injected Storage, so vitest can verify the persistence
 * contract (device_id survives reloads, session_id stays per-tab) in a node
 * environment without a DOM mock.
 *
 * D7 decision: device_id is the anonymous cross-visit identity; the backend
 * scopes conversation state by `device_id:session_id`, so it must be created
 * once and never regenerated per visit.
 */

/** LocalStorage key for the persistent anonymous device id (created once). */
export const DEVICE_ID_KEY = "ragre_device_id";
/** Legacy key kept for one-time migration from the previous dotted name. */
const LEGACY_DEVICE_ID_KEY = "ragre.device_id";

/** SessionStorage key for the per-tab chat session id (unchanged from Epic 5). */
export const SESSION_KEY = "ragre.session_id";

/**
 * SessionStorage key for the per-tab TRAINING session id. The backend scopes
 * chat_sessions by session_id and refuses to bind a training turn onto an id
 * that already exists as a customer conversation (409), so the two surfaces
 * MUST NOT share one key. Customer keeps the legacy `ragre.session_id` (no
 * migration); training gets this distinct, suffixed key.
 */
export const SESSION_KEY_TRAINING = "ragre.session_id.training";

/** Which persisted session-id namespace a call site reads/writes. */
export type SessionScope = "customer" | "training";

/** Maps a scope to its SessionStorage key (customer keeps the legacy key). */
export function sessionKeyForScope(scope: SessionScope): string {
  return scope === "training" ? SESSION_KEY_TRAINING : SESSION_KEY;
}

/** LocalStorage key for the last chosen active project (story 10.3). */
export const PROJECT_KEY_STORAGE = "ragre.project_key";

/**
 * LocalStorage key for the server-minted HMAC-signed anonymous identity
 * token (secure-wave spec §6). The server is the issuer and verifier; the FE
 * only stores it durably (localStorage, not tab-scoped) and echoes it back on
 * every /query so quota state survives restarts and reloads.
 */
export const ANON_TOKEN_KEY = "ragre.anon_token";

/** Minimal Storage subset so tests can inject a plain-object mock. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Loads the persistent device id, creating a fresh UUID v4 on first visit. */
export function getDeviceId(storage: StorageLike): string {
  const existing = storage.getItem(DEVICE_ID_KEY) ?? storage.getItem(LEGACY_DEVICE_ID_KEY);
  if (existing) {
    if (!storage.getItem(DEVICE_ID_KEY)) storage.setItem(DEVICE_ID_KEY, existing);
    return existing;
  }
  const fresh = crypto.randomUUID();
  storage.setItem(DEVICE_ID_KEY, fresh);
  return fresh;
}

/**
 * Loads the session id for the current tab. With forceNew=true a fresh UUID is
 * minted, which is how a project change resets the conversation context on the
 * backend (`device_id:session_id` scope key) instead of continuing the old one.
 */
export function getSessionId(storage: StorageLike, forceNew = false): string {
  return getSessionIdForScope(storage, "customer", forceNew);
}

/**
 * Scope-aware session id. Customer resolves the legacy `ragre.session_id` key
 * (byte-identical to getSessionId); training resolves the distinct
 * `ragre.session_id.training` key so a training turn can never collide with a
 * customer conversation's session id (the backend 409 root cause). forceNew
 * mints a fresh id within the SAME scope (a project change keeps resetting the
 * training context against a new training session id, never the customer one).
 */
export function getSessionIdForScope(
  storage: StorageLike,
  scope: SessionScope,
  forceNew = false
): string {
  const key = sessionKeyForScope(scope);
  const existing = storage.getItem(key);
  if (existing && !forceNew) return existing;
  const fresh = crypto.randomUUID();
  storage.setItem(key, fresh);
  return fresh;
}

/** Returns the last project the user picked, or null when none was stored. */
export function getStoredProjectKey(storage: StorageLike): string | null {
  return storage.getItem(PROJECT_KEY_STORAGE);
}

/** Persists the project choice so returning visitors skip the picker. */
export function storeProjectKey(storage: StorageLike, projectKey: string): void {
  storage.setItem(PROJECT_KEY_STORAGE, projectKey);
}

/**
 * Returns the persisted server-minted anon token, or null when none was
 * stored yet (the backend mints one and returns it on the next /query).
 */
export function getAnonToken(storage: StorageLike): string | null {
  return storage.getItem(ANON_TOKEN_KEY);
}

/**
 * Self-heal write path: persists an anon token returned by any /query
 * response, SSE ack frame, or GET /api/anon/token. Non-string or empty input
 * is ignored (returns false) so a malformed payload can never clobber a good
 * stored token.
 */
export function persistAnonToken(storage: StorageLike, token: unknown): boolean {
  if (typeof token !== "string" || token.length === 0) return false;
  storage.setItem(ANON_TOKEN_KEY, token);
  return true;
}
