/**
 * Best-effort Web Notification API helper for incoming CRM leads (story 9.3).
 *
 * Frozen decision: NO FCM/push service — notifications are fired locally by
 * the open CRM tab only. Permission may ONLY be requested from a user gesture
 * (the "Bật thông báo" button); everything is guarded so unsupported
 * environments (SSR, older browsers, denied permission) degrade silently.
 */

export type BrowserNotificationPermission =
  | NotificationPermission
  | "unsupported";

export type BrowserNotificationPreference = "unset" | "enabled" | "denied";

const NOTIFICATION_PREFERENCE_KEY = "crm.browser-notifications.preference";

export function browserNotificationsSupported(): boolean {
  return typeof window !== "undefined" && typeof window.Notification === "function";
}

/** Current permission, or "unsupported" outside browsers lacking the API. */
export function browserLeadNotificationPermission(): BrowserNotificationPermission {
  if (!browserNotificationsSupported()) {
    return "unsupported";
  }
  return window.Notification.permission;
}

export function browserLeadNotificationPreference(): BrowserNotificationPreference {
  if (typeof window === "undefined") return "unset";
  try {
    const value = window.localStorage.getItem(NOTIFICATION_PREFERENCE_KEY);
    return value === "enabled" || value === "denied" ? value : "unset";
  } catch {
    return "unset";
  }
}

export function persistBrowserLeadNotificationPreference(
  preference: Exclude<BrowserNotificationPreference, "unset">
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(NOTIFICATION_PREFERENCE_KEY, preference);
  } catch {
    // Storage can be unavailable in privacy-restricted browsing contexts.
  }
}

/**
 * MUST be called from a user gesture handler (click) — browsers otherwise
 * auto-deny. Returns the resulting permission, or "unsupported".
 */
export async function requestBrowserLeadNotificationPermission(): Promise<BrowserNotificationPermission> {
  if (!browserNotificationsSupported()) {
    return "unsupported";
  }
  try {
    return await window.Notification.requestPermission();
  } catch {
    return window.Notification.permission;
  }
}

/** Fires a local notification when granted; any failure is swallowed. */
export function showBrowserLeadNotification(request: {
  title: string;
  body: string;
}): void {
  if (!browserNotificationsSupported()) {
    return;
  }
  if (window.Notification.permission !== "granted") {
    return;
  }
  try {
    new window.Notification(request.title, { body: request.body });
  } catch {
    // Best-effort only (some platforms restrict the constructor); the antd
    // toast fired alongside remains the reliable channel.
  }
}
