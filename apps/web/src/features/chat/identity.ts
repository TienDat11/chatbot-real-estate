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
export const DEVICE_ID_KEY = "ragre.device_id";

/** SessionStorage key for the per-tab chat session id (unchanged from Epic 5). */
export const SESSION_KEY = "ragre.session_id";

/** LocalStorage key for the last chosen active project (story 10.3). */
export const PROJECT_KEY_STORAGE = "ragre.project_key";

/** Minimal Storage subset so tests can inject a plain-object mock. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Loads the persistent device id, creating a fresh UUID v4 on first visit. */
export function getDeviceId(storage: StorageLike): string {
  const existing = storage.getItem(DEVICE_ID_KEY);
  if (existing) return existing;
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
  const existing = storage.getItem(SESSION_KEY);
  if (existing && !forceNew) return existing;
  const fresh = crypto.randomUUID();
  storage.setItem(SESSION_KEY, fresh);
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
