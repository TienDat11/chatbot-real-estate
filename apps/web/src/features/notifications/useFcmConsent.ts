"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { registerFcmToken } from "@/infrastructure/firebase/firebaseMessaging";
import { getAnonToken } from "@/features/chat/identity";

const CONSENT_KEY = "ragre.fcm.consent";
const TOKEN_KEY = "ragre.fcm.last_token";
const LAST_REGISTERED_AT_KEY = "ragre.fcm.last_registered_at";
const ANON_RETRY_DELAY_MS = 2000;
// A stored token string matching localStorage is NOT proof the backend row is
// still enabled: server-side pruning (e.g. prune_token on an FCM rejection) or
// a manual DB fix can silently disable delivery while the client keeps skipping
// the POST. Re-POST at least every 7 days so a pruned row heals on the next
// mount; a fresh mount after a change is always immediate (missing timestamp).
const REGISTRATION_REFRESH_MS = 7 * 24 * 60 * 60 * 1000;

// Identity-cancellation sentinel: registerDeviceToken rejects with this unique
// object (never a Response) when the epoch moved on before a POST was issued,
// so the request is never made for a stale identity. Callers already swallow
// registration errors (push must never block the primary surface), which makes
// cancellation a silent no-op.
const CANCELLED = Symbol("fcm-registration-cancelled");

// The browser's native Notification.permission is the hard gate: a 'denied'
// permission means the OS/browser has blocked delivery, so no in-app consent
// can make notifications live. 'granted' and 'default' are the two states the
// in-app consent toggle operates on.
function readNativePermission(): NotificationPermission {
  return typeof Notification !== "undefined" ? Notification.permission : "default";
}

// Consent and the persisted device token are scoped by Firebase UID, not just
// by surface mode. Keying on mode alone let a second account inherit the first
// account's enabled state and reuse its registered token after a logout/login
// in the same browser (the token is bound server-side to whoever registered
// it). Every read/write is namespaced `.<mode>.<uid>`; when no UID is available
// (signed out, or an anonymous client visitor) the surface is inert: nothing is
// restored and nothing is persisted, so a logged-out render can never carry a
// previous account's consent.
function consentKey(mode: "sales" | "client", uid: string): string {
  return `${CONSENT_KEY}.${mode}.${uid}`;
}
function tokenKey(mode: "sales" | "client", uid: string): string {
  return `${TOKEN_KEY}.${mode}.${uid}`;
}
function lastRegisteredKey(mode: "sales" | "client", uid: string): string {
  return `${LAST_REGISTERED_AT_KEY}.${mode}.${uid}`;
}

// POST /api/notifications/device-token for the active surface. In client mode
// a 401/403 usually means the signed anon token was minted but not yet
// readable in storage (registration race), so the token is re-read fresh and
// the POST is retried EXACTLY ONCE after a short backoff. Other statuses and
// DELETE never retry. The identity epoch is re-checked immediately before
// EVERY POST (initial and retry): a stale call whose earlier await boundary
// resolved after the identity moved on must never issue a request, otherwise
// account A's token could be POSTed under account B's bearer. A cancellation
// detected at this point rejects with the CANCELLED sentinel (no request was
// made), which callers treat as a silent no-op.
async function registerDeviceToken(
  mode: "sales" | "client",
  token: string,
  isCancelled: () => boolean,
): Promise<boolean> {
  const post = async (): Promise<Response> => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (mode === "sales") headers.Authorization = `Bearer ${await getFreshIdToken()}`;
    else { const anon = getAnonToken(window.localStorage); if (anon) headers["X-Anon-Token"] = anon; }
    // The bearer/anon read above is an await boundary: the identity epoch can
    // move on while it is in flight. Re-check immediately before issuing the
    // fetch so a stale call can never POST under the new account's bearer.
    if (isCancelled()) throw CANCELLED;
    return fetch("/api/notifications/device-token", { method: "POST", headers, body: JSON.stringify({ token, platform: "web" }) });
  };
  let response = await (isCancelled() ? Promise.reject(CANCELLED) : post());
  if (!response.ok && mode === "client" && (response.status === 401 || response.status === 403)) {
    await new Promise((resolve) => setTimeout(resolve, ANON_RETRY_DELAY_MS));
    if (isCancelled()) return false;
    response = await (isCancelled() ? Promise.reject(CANCELLED) : post());
  }
  return response.ok;
}

// Unregister and drop the stored device token for a uid. Shared by the
// toggle-off path and every denial-normalization path so persisted state, the
// effective badge, and the browser permission can never disagree: if a token
// was stored it is DELETEd from the backend (best effort), then the scoped
// token key is removed. When no token was stored nothing is sent, so the
// backend never sees an empty-token 422. The epoch guard (isCancelled) aborts
// the DELETE if the Firebase identity moved on mid-flight.
async function unregisterStoredToken(
  mode: "sales" | "client",
  uid: string,
  isCancelled: () => boolean,
): Promise<void> {
  let storedToken: string | null = null;
  try { storedToken = window.localStorage.getItem(tokenKey(mode, uid)); } catch { /* storage unavailable */ }
  if (!storedToken) return;
  // Remove the scoped key synchronously BEFORE the async DELETE. A prompt
  // denial can fire this from two paths at once (PermissionStatus.onchange and
  // the unconditional denial branch in change()); without this, both read the
  // stored token before either removes it and issue two DELETEs. Removing first
  // makes the second call read null and skip, so unregistration is effectively
  // single-flight per uid. The captured token still drives the idempotent DELETE.
  try { window.localStorage.removeItem(tokenKey(mode, uid)); } catch { /* storage unavailable */ }
  // Drop the registration timestamp with the token: a consent-off path must not
  // leave a fresh timestamp that would make a later re-enable skip the POST.
  try { window.localStorage.removeItem(lastRegisteredKey(mode, uid)); } catch { /* storage unavailable */ }
  try {
    // Content-Type is required: FastAPI rejects a JSON body sent without it
    // as 422 (body never parsed), which was the observed consent-off failure.
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (mode === "sales") headers.Authorization = `Bearer ${await getFreshIdToken()}`;
    else { const anon = getAnonToken(window.localStorage); if (anon) headers["X-Anon-Token"] = anon; }
    if (isCancelled()) return;
    await fetch("/api/notifications/device-token", { method: "DELETE", headers, body: JSON.stringify({ token: storedToken }) });
  } catch { /* unregistration is best effort */ }
}

// Cleanup of the pre-UID keys. Legacy consent lived at
// `ragre.fcm.consent.<mode>` (and, before that, the un-namespaced
// `ragre.fcm.consent`); the legacy token at `ragre.fcm.last_token.<mode>`.
// These keys are deleted unconditionally and NEVER read into state: the old
// automatic migration copied an ownerless legacy value into whichever UID
// first mounted after the upgrade, which is a cross-account disclosure — on a
// shared browser, account B logging in after the upgrade would silently
// inherit account A's consent and registered token. The scoped keys are the
// only source of truth; a missing scoped key means disabled/no-token, so
// every user must explicitly re-consent after the upgrade.
function clearLegacyKeys(storage: Storage): void {
  try {
    storage.removeItem(`${CONSENT_KEY}.sales`);
    storage.removeItem(`${CONSENT_KEY}.client`);
    storage.removeItem(CONSENT_KEY);
    storage.removeItem(`${TOKEN_KEY}.sales`);
    storage.removeItem(`${TOKEN_KEY}.client`);
  } catch { /* private storage */ }
}

// Restore the durable truth for the current uid. The consent flag alone is
// fragile: it can be cleared (private-storage eviction, a partial write) while
// the user is still genuinely opted in — the browser permission is granted and
// a device token was registered. Treat "permission granted AND a token was
// stored for THIS uid" as enabled so closing and reopening the app no longer
// shows OFF for a user who enabled it. An explicit stored consent still wins; a
// granted permission with no stored token for this uid stays OFF (the user
// never completed registration).
function restoreEnabled(storage: Storage, mode: "sales" | "client", uid: string): boolean {
  try {
    if (storage.getItem(consentKey(mode, uid)) === "true") return true;
    const permissionGranted = typeof Notification !== "undefined" && Notification.permission === "granted";
    return permissionGranted && storage.getItem(tokenKey(mode, uid)) !== null;
  } catch {
    return false;
  }
}

export function useFcmConsent(mode: "sales" | "client", active: boolean, uid: string | null) {
  // `consent` is the in-app stored toggle (the durable user intent). The
  // effective `enabled` is derived below from consent AND the native browser
  // permission, so the badge can never show "on" while the browser blocks it.
  const [consent, setConsent] = useState(false);
  // Deterministic initial value for SSR: the server always renders "default"
  // and the browser's real Notification.permission is read in a mount effect
  // (see refreshNativePermission below). Reading it during render here would
  // make the server HTML ("default") disagree with the first client render
  // ("granted"/"denied") and produce a hydration mismatch on the badge label,
  // dot color, and aria attributes.
  const [nativePermission, setNativePermission] = useState<NotificationPermission>("default");
  const [loading, setLoading] = useState(false);
  // True once the mount effect has read the real browser permission. Until then
  // the control renders a stable neutral placeholder so server and client agree.
  const [hydrated, setHydrated] = useState(false);
  // The uid whose durable consent state has been restored by the restore
  // effect below. The register/sync effect is gated on this so it can never
  // run with a stale `enabled` closure for a NEW uid: on an identity switch,
  // effects run in declaration order within the same commit, so the sync
  // effect still sees the PREVIOUS account's `enabled === true` until the
  // state update from the restore effect commits. Until `restoredFor` matches
  // `uid`, the sync effect must not register anything for the new identity.
  const [restoredFor, setRestoredFor] = useState<string | null>(null);
  const previousActive = useRef(active);
  // Last permission value observed by refreshNativePermission, used to detect a
  // transition INTO 'denied' (external revoke) so persisted state is normalized
  // exactly once rather than on every focus/visibility event.
  const nativePermissionRef = useRef<NotificationPermission>("default");
  // Identity epoch: bumped whenever the Firebase UID changes. Every async
  // register/delete captures the epoch at start and aborts if it has moved on,
  // so work started for account A can never persist A's token or fire a DELETE
  // that lands on account B after a switch.
  const epochRef = useRef(0);
  // Last non-null UID seen. On sign-out the caller passes uid=null at the same
  // moment active flips false, so the logout DELETE must target the account
  // that just left, not the (now absent) current uid.
  const lastUidRef = useRef<string | null>(uid);
  // Single place that re-reads Notification.permission into state and, on a
  // transition INTO 'denied', normalizes persisted state so consent key, badge,
  // and browser permission cannot disagree. Called from the Permissions API
  // subscription (instant path on supported browsers), the mount effect, and the
  // visibility/focus fallback (for browsers where the Permissions API is
  // missing/rejected and onchange never fires). The transition guard means an
  // already-denied permission does not re-persist or re-DELETE on every event.
  const refreshNativePermission = useCallback(() => {
    const permission = readNativePermission();
    const previous = nativePermissionRef.current;
    nativePermissionRef.current = permission;
    setNativePermission(permission);
    if (permission === "denied" && previous !== "denied" && uid) {
      const normalizeUid = uid;
      const normalizeEpoch = epochRef.current;
      const isCancelled = () => epochRef.current !== normalizeEpoch;
      setConsent(false);
      try { window.localStorage.setItem(consentKey(mode, normalizeUid), "false"); } catch { /* private storage */ }
      void unregisterStoredToken(mode, normalizeUid, isCancelled);
    }
  }, [mode, uid]);
  // Keep a ref to the latest refresh so the once-per-mount subscriptions below
  // (Permissions API, visibility/focus) always invoke the current uid/mode
  // closure without re-subscribing on every identity change.
  const refreshRef = useRef(refreshNativePermission);
  useEffect(() => { refreshRef.current = refreshNativePermission; }, [refreshNativePermission]);
  // Restore the durable truth for the CURRENT uid only. Runs on mount and on
  // every uid/mode change; a null uid (signed out / anon) resets to OFF and
  // persists nothing, so a previous account's state never carries over.
  useEffect(() => {
    epochRef.current += 1;
    // Identity switched: start the transition-into-denied guard fresh for this
    // uid. Without this reset, a stale async change() from a previous account
    // could leave nativePermissionRef at "denied", so this account's mount
    // refresh would see previous === "denied" and skip denial normalization,
    // persisting a "true" consent that contradicts a denied browser permission.
    nativePermissionRef.current = "default";
    if (uid) lastUidRef.current = uid;
    // Drop any consent carried over from the previous account SYNCHRONOUSLY,
    // before the restore read below, so the new uid never inherits A's
    // `enabled`. The restore read then sets the correct consent for this uid
    // in the same effect. `restoredFor` is reset first and only set to `uid`
    // once the durable truth has been read, which is what actually blocks the
    // sync effect from firing with A's stale `enabled` closure in this commit.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setConsent(false);
    setRestoredFor(null);
    if (!uid) return;
    try { clearLegacyKeys(window.localStorage); setConsent(restoreEnabled(window.localStorage, mode, uid)); } catch { /* private storage */ }
    setRestoredFor(uid);
    // Re-read the real browser permission for THIS uid now that the transition
    // guard was reset above. The once-per-mount focus/visibility effect (deps
    // []) does not re-run on an identity switch, so without this the new uid's
    // permission would never be observed and a genuine denial for the current
    // uid would not be normalized. The transition guard still ensures the
    // normalization runs exactly once (previous "default" -> "denied").
    refreshRef.current();
  }, [mode, uid]);
  // Keep the badge live when the user changes the OS/browser permission while
  // the tab is open: subscribe to the Permissions API 'notifications' status so
  // a revoke/allow outside the app flips nativePermission (and therefore the
  // derived `enabled`) without a reload. The API is optional — where it is
  // unavailable the visibility/focus fallback below still re-reads the
  // permission, so an external revoke is never left stale until reload.
  useEffect(() => {
    if (typeof navigator === "undefined" || !navigator.permissions?.query) return;
    let cancelled = false;
    let status: PermissionStatus | undefined;
    void navigator.permissions
      .query({ name: "notifications" as PermissionName })
      .then((result) => {
        if (cancelled) return;
        status = result;
        refreshRef.current();
        result.onchange = () => { refreshRef.current(); };
      })
      .catch(() => { /* some browsers reject the notifications name */ });
    return () => { cancelled = true; if (status) status.onchange = null; };
  }, []);
  // Fallback for browsers without a working Permissions API: re-read the native
  // permission whenever the tab regains focus or becomes visible. This is the
  // only path that catches an OS/browser-settings revoke on those browsers, and
  // it also performs the mount-time read that clears the SSR hydration mismatch.
  useEffect(() => {
    refreshRef.current();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHydrated(true);
    const onRevisible = () => {
      if (typeof document === "undefined" || document.visibilityState === "visible") refreshRef.current();
    };
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onRevisible);
    if (typeof window !== "undefined") window.addEventListener("focus", onRevisible);
    return () => {
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onRevisible);
      if (typeof window !== "undefined") window.removeEventListener("focus", onRevisible);
    };
  }, []);
  // Single source of truth for the badge: notifications are live only when the
  // in-app consent is on AND the browser has not blocked them. A 'denied'
  // permission is a hard block that no toggle can override; 'granted' and
  // 'default' both honour the stored consent (a 'default' consent still means
  // the user opted in and the browser has not refused).
  const enabled = consent && nativePermission !== "denied";
  useEffect(() => {
    const cleanupUid = lastUidRef.current;
    if (previousActive.current && !active && mode === "sales" && cleanupUid) {
      const cleanupEpoch = epochRef.current;
      const cleanup = async () => {
        try {
          const token = window.localStorage.getItem(tokenKey(mode, cleanupUid));
          if (!token) return;
          // Drop the scoped token key synchronously BEFORE awaiting the bearer and
          // backend DELETE. The sync effect skips registration when the stored
          // token matches a fresh FCM token, so leaving the stale key behind would
          // make the badge read as on after re-login while the backend has no
          // registration for this device. Removing first forces re-registration.
          try { window.localStorage.removeItem(tokenKey(mode, cleanupUid)); } catch { /* storage unavailable */ }
          const bearer = await getFreshIdToken();
          // Identity moved on while the token/bearer reads were in flight: a
          // DELETE now would target the wrong account, so drop it.
          if (epochRef.current !== cleanupEpoch) return;
          await fetch("/api/notifications/device-token", { method: "DELETE", headers: { "Content-Type": "application/json", Authorization: `Bearer ${bearer}` }, body: JSON.stringify({ token }) });
        } catch { /* logout cleanup is best effort */ }
      };
      void cleanup();
    }
    previousActive.current = active;
    // Do not register until the real browser permission has been read: before
    // hydration nativePermission is the deterministic "default", so a restored
    // consent could briefly read as enabled for a user whose browser actually
    // denied, and fire a pointless registration.
    // Do not register until the durable consent state for THIS uid has been
    // restored: on an identity switch the sync effect's `enabled` closure can
    // still hold the previous account's value for one commit, and registering
    // then would transiently treat the new uid as consented.
    if (!active || !enabled || !uid || !hydrated || restoredFor !== uid) return;
    const syncUid = uid;
    const syncEpoch = epochRef.current;
    let cancelled = false;
    const isCancelled = () => cancelled || epochRef.current !== syncEpoch;
    const sync = async () => {
      const token = await registerFcmToken();
      if (!token || isCancelled()) return;
      let last: string | null = null;
      let lastRegisteredAt: number | null = null;
      try {
        last = window.localStorage.getItem(tokenKey(mode, syncUid));
        const rawAt = window.localStorage.getItem(lastRegisteredKey(mode, syncUid));
        lastRegisteredAt = rawAt === null ? null : Number(rawAt);
      } catch { /* storage unavailable */ }
      // POST when the token string changed OR the last successful registration
      // is missing/unparseable/older than the refresh window. The unchanged-
      // token-but-stale case is the heal: it re-arms a backend row that was
      // pruned/disabled server-side while localStorage still matched.
      const stale =
        lastRegisteredAt === null ||
        !Number.isFinite(lastRegisteredAt) ||
        Date.now() - lastRegisteredAt >= REGISTRATION_REFRESH_MS;
      if (last === token && !stale) return;
      if (await registerDeviceToken(mode, token, isCancelled)) {
        if (isCancelled()) return;
        try {
          window.localStorage.setItem(tokenKey(mode, syncUid), token);
          window.localStorage.setItem(lastRegisteredKey(mode, syncUid), String(Date.now()));
        } catch { /* storage unavailable */ }
      }
    };
    void sync().catch(() => undefined);
    return () => { cancelled = true; };
  }, [active, enabled, mode, uid, hydrated, restoredFor]);
  const change = async (next: boolean) => {
    if (!uid) return;
    const changeUid = uid;
    const changeEpoch = epochRef.current;
    const isCancelled = () => epochRef.current !== changeEpoch;
    setConsent(next); setLoading(true);
    let token: string | null = null;
    try {
      try { window.localStorage.setItem(consentKey(mode, changeUid), String(next)); } catch { /* private storage */ }
      // Identity moved on before this change started its work: do not prompt
      // the browser or register anything at all for a stale toggle. (The
      // epoch cannot have moved on this synchronous path today, but the guard
      // pins the invariant and protects any future await added above this
      // line; the registerDeviceToken pre-POST check covers the async window.)
      if (next && isCancelled()) return;
      token = next ? await registerFcmToken({ requestPermission: true }) : null;
    } catch {
      // Push registration must never block the primary surface. Swallow here but
      // still run the permission normalization below: a rejected register (e.g.
      // the browser denied and threw) must not leave a stale persisted "true".
    }
    // Re-read the native permission AFTER the (possible) prompt regardless of
    // whether registration succeeded or threw, so the badge reflects the
    // browser's real answer and a denial is normalized even when the Permissions
    // API is unavailable (the only other re-read path).
    const permission = readNativePermission();
    // Only adopt the freshly-read permission into the transition guard/state
    // when this change still owns the identity epoch. A stale/cancelled change
    // for account A must NOT mutate nativePermissionRef/state that now belongs
    // to account B (the mount/focus refresh re-reads the real value for B).
    if (!isCancelled()) {
      nativePermissionRef.current = permission;
      setNativePermission(permission);
    }
    if (permission === "denied") {
      // The browser refused (or was already blocked): revert the in-app consent,
      // persist "false", and drop any stored token (+ backend DELETE) so consent
      // key, effective badge, and browser permission cannot disagree.
      if (!isCancelled()) setConsent(false);
      try { window.localStorage.setItem(consentKey(mode, changeUid), "false"); } catch { /* private storage */ }
      await unregisterStoredToken(mode, changeUid, isCancelled);
      if (!isCancelled()) setLoading(false);
      return;
    }
    try {
      if (next && token) {
        if (await registerDeviceToken(mode, token, isCancelled)) {
          if (isCancelled()) return;
          // Record the registration time alongside the token so the sync effect
          // (which fires in this same commit once `enabled` flips true) does not
          // immediately re-POST the unchanged token it just saved.
          try {
            window.localStorage.setItem(tokenKey(mode, changeUid), token);
            window.localStorage.setItem(lastRegisteredKey(mode, changeUid), String(Date.now()));
          } catch { /* storage unavailable */ }
        }
      } else if (!next) {
        await unregisterStoredToken(mode, changeUid, isCancelled);
      }
    } catch { /* Push registration must never block the primary surface. */ }
    finally { if (!isCancelled()) setLoading(false); }
  };
  return { enabled, loading, nativePermission, hydrated, change };
}
