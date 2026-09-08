"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { App } from "antd";
import type { Lead } from "@/domain/crm/lead";
import { useAuth } from "@/lib/AuthProvider";
import { CrmLeadStreamProvider } from "@/features/crm/useCrmLeadStream";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { subscribeToFcmMessages } from "@/infrastructure/firebase/firebaseMessaging";
import { sameOriginNotificationPath } from "@/infrastructure/firebase/firebaseNotificationUtils";
import { NotificationConsentControl } from "@/features/notifications/NotificationConsentControl";
import { useFcmConsent } from "@/features/notifications/useFcmConsent";
import { showBrowserLeadNotification } from "@/features/crm/browserLeadNotifier";

export interface SalesNotification { id: string; lead_id: number; project_key: string; masked_phone: string | null; display_name: string | null; created_at: string; read_at: string | null }
interface NotificationContextValue { items: SalesNotification[]; unreadCount: number; error: string | null; markRead: (id: string) => Promise<void>; markAllRead: () => Promise<void>; }
const NotificationContext = createContext<NotificationContextValue | null>(null);

async function request(path: string, init?: RequestInit): Promise<Record<string, unknown>> {
  const token = await getFreshIdToken();
  const response = await fetch(path, { ...init, headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) } });
  if (!response.ok) throw new Error("Không thể cập nhật thông báo.");
  return response.json() as Promise<Record<string, unknown>>;
}

function leadNotification(lead: Lead): SalesNotification {
  const numericId = Number(lead.leadId);
  const leadId = Number.isSafeInteger(numericId) ? numericId : 0;
  // The read-model id domain is the backend's str(leads.id) (decimal); a live
  // item must share it — lead.id is an HMAC hex digest, and PATCHing it gets
  // 422 while the same lead never merges with its server row.
  return { id: leadId > 0 ? String(leadId) : lead.id, lead_id: leadId, project_key: lead.projectKey, masked_phone: lead.maskedPhone, display_name: lead.name, created_at: lead.createdAt, read_at: null };
}

function mergeNotifications(current: SalesNotification[], incoming: SalesNotification[]): SalesNotification[] {
  const byId = new Map(current.map((item) => [item.id, item]));
  for (const item of incoming) byId.set(item.id, { ...byId.get(item.id), ...item });
  return [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export function SalesNotificationProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { notification } = App.useApp();
  const [items, setItems] = useState<SalesNotification[]>([]);
  const [serverUnreadCount, setServerUnreadCount] = useState(0);
  const [liveUnreadIds, setLiveUnreadIds] = useState<Set<string>>(() => new Set());
  const [localReadIds, setLocalReadIds] = useState<Set<string>>(() => new Set());
  const [readMutationIds, setReadMutationIds] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const activeUidRef = useRef<string | null>(null);
  const serverItemIdsRef = useRef<Set<string>>(new Set());
  // Shared throttle timestamp for window-focus/visibility refetches; any
  // refetch stamps it so a message burst plus a focus event cannot storm the API.
  const lastRefetchAtRef = useRef(0);
  const refetchInFlightRef = useRef(false);
  const uid = auth.user?.uid ?? null;
  const localReadIdsRef = useRef(localReadIds);
  const readMutationIdsRef = useRef(readMutationIds);
  useEffect(() => { localReadIdsRef.current = localReadIds; }, [localReadIds]);
  useEffect(() => { readMutationIdsRef.current = readMutationIds; }, [readMutationIds]);
  const onIncomingLead = useCallback((lead: Lead) => {
    const item = leadNotification(lead);
    if (item.lead_id <= 0 || !uid || activeUidRef.current !== uid) return;
    setItems((previous) => mergeNotifications(previous, [item]));
    if (!serverItemIdsRef.current.has(item.id)) setLiveUnreadIds((previous) => new Set(previous).add(item.id));
    const description = item.display_name ?? "Có lead mới được gán cho bạn.";
    notification.open({ message: "Lead mới", description, duration: 5 });
    showBrowserLeadNotification({ title: "Lead mới", body: description });
  }, [notification, uid]);
  const role = auth.user?.role;
  const consent = useFcmConsent("sales", Boolean(uid && !auth.loading && role === "sales"), uid);
  const streamOptions = useMemo(() => ({ assignedSalesFirebaseUidFilter: role === "sales" ? uid : null, onIncomingLead: role === "sales" ? onIncomingLead : undefined }), [uid, onIncomingLead, role]);
  // Indirection so the baseline-retry below can re-invoke refetchList without
  // referencing it inside its own useCallback initializer (react-hooks flags a
  // const read before its declaration completes). The ref is populated in an
  // effect, so the retry always calls the current stable instance.
  const refetchListRef = useRef<(options?: { baseline?: boolean; isCancelled?: () => boolean }) => Promise<void>>(async () => {});
  const refetchList = useCallback(async (options: { baseline?: boolean; isCancelled?: () => boolean } = {}) => {
    const baseline = options.baseline === true;
    const isCancelled = options.isCancelled ?? (() => false);
    const uid = activeUidRef.current;
    if (!uid || isCancelled()) return;
    // Coalesce event-driven refetches: a message burst plus a focus event
    // would otherwise storm the API with identical GETs. The baseline uid-
    // effect fetch always runs so a uid switch never misses its baseline.
    if (!baseline) {
      if (refetchInFlightRef.current) return;
      refetchInFlightRef.current = true;
    }
    lastRefetchAtRef.current = Date.now();
    try {
      const body = await request("/api/sales/notifications?status=unread&limit=100");
      if (isCancelled() || activeUidRef.current !== uid) return;
      const serverItems = Array.isArray(body.items) ? body.items as SalesNotification[] : [];
      const serverIds = new Set(serverItems.map((item) => item.id));
      serverItemIdsRef.current = serverIds;
      const visibleItems = serverItems.map((item) => localReadIdsRef.current.has(item.id) ? { ...item, read_at: item.read_at ?? new Date().toISOString() } : item);
      setItems((previous) => mergeNotifications(previous, visibleItems));
      setLiveUnreadIds((previous) => {
        const next = new Set(previous);
        for (const item of serverItems) next.delete(item.id);
        return next;
      });
      if (typeof body.unread_count === "number") {
        setServerUnreadCount(Math.max(0, body.unread_count));
        setLocalReadIds((previous) => new Set([...previous].filter((id) => serverIds.has(id))));
      }
      setError(null);
    } catch {
      if (isCancelled() || activeUidRef.current !== uid) return;
      // A refetch failure keeps the current items (merge never resets to
      // empty); only the baseline mount retries and shows the load error.
      setError(baseline ? "Không thể tải thông báo." : "Không thể làm mới thông báo.");
      if (baseline) window.setTimeout(() => { if (!isCancelled()) void refetchListRef.current({ baseline: true, isCancelled }); }, 500);
    } finally {
      if (!baseline) refetchInFlightRef.current = false;
    }
  }, []);
  useEffect(() => { refetchListRef.current = refetchList; }, [refetchList]);
  const baselineCancelRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (activeUidRef.current !== uid) {
      // Auth-scope reset lives here (not in render): render-phase setState is
      // fragile under StrictMode/concurrent rendering. Clearing synchronously
      // before the baseline refetch guarantees user A's items/unread state can
      // never leak into user B's session, even for one frame.
      serverItemIdsRef.current = new Set();
      setItems([]);
      setServerUnreadCount(0);
      setLiveUnreadIds(new Set());
      setLocalReadIds(new Set());
      setReadMutationIds(new Set());
      setError(null);
    }
    activeUidRef.current = uid;
    baselineCancelRef.current?.();
    if (!uid || auth.loading || role !== "sales") { baselineCancelRef.current = null; return; }
    let cancelled = false;
    baselineCancelRef.current = () => { cancelled = true; };
    void refetchList({ baseline: true, isCancelled: () => cancelled });
    return () => { cancelled = true; baselineCancelRef.current = null; };
  }, [auth.loading, refetchList, role, uid]);
  useEffect(() => {
    if (!uid || auth.loading || role !== "sales") return;
    // Focus/visibility refetch is throttled (>= 30s between refetches, ref
    // timestamp shared with message-driven refetches) so tab switching cannot
    // hammer the unread feed.
    const throttledRefetch = () => {
      if (Date.now() - lastRefetchAtRef.current < 30_000) return;
      void refetchList();
    };
    const onVisibility = () => { if (document.visibilityState === "visible") throttledRefetch(); };
    window.addEventListener("focus", throttledRefetch);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", throttledRefetch);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [auth.loading, refetchList, role, uid]);
  useEffect(() => {
    if (!uid || auth.loading || role !== "sales") return;
    let cancelled = false;
    let unsubscribe: () => void = () => {};
    void subscribeToFcmMessages((payload) => {
      // Only the sales-relevant lead event opens a toast and refetches the
      // unread feed. Unknown, malformed, or client-targeted payloads
      // (sales_call_started) are ignored: no toast, no API churn. The body
      // text is authored by the backend (masked phone only); no raw data
      // fields are echoed into the toast surface.
      if (payload.data?.type !== "new_lead") return;
      const title = payload.notification?.title ?? "Lead mới";
      const body = payload.notification?.body ?? "";
      notification.open({ message: title, description: body, duration: 5, onClick: () => { window.location.assign(sameOriginNotificationPath(payload.data?.url)); } });
      // A foreground push usually means new unread server rows; refresh the
      // unread feed so the badge stays truthful without waiting for focus.
      void refetchList();
    }).then((stop) => { if (cancelled) stop(); else unsubscribe = stop; });
    return () => { cancelled = true; unsubscribe(); };
  }, [auth.loading, notification, refetchList, role, uid]);
  const markRead = useCallback(async (id: string) => {
    if (readMutationIdsRef.current.has(id)) return;
    const item = items.find((candidate) => candidate.id === id);
    if (!item || item.read_at) return;
    readMutationIdsRef.current = new Set(readMutationIdsRef.current).add(id);
    setReadMutationIds(readMutationIdsRef.current);
    try {
      await request("/api/sales/notifications/read", { method: "PATCH", body: JSON.stringify({ notification_ids: [id], read: true }) });
      const readAt = new Date().toISOString();
      setItems((previous) => previous.map((candidate) => candidate.id === id ? { ...candidate, read_at: candidate.read_at ?? readAt } : candidate));
      setLocalReadIds((previous) => new Set(previous).add(id));
      setLiveUnreadIds((previous) => { const next = new Set(previous); next.delete(id); return next; });
      if (serverItemIdsRef.current.has(id)) {
        setServerUnreadCount((count) => Math.max(0, count - 1));
      }
      setError(null);
    } catch {
      // Callers invoke markRead with `void`, so a rethrow becomes an unhandled
      // rejection: surface it and keep badge/items untouched so the idempotent
      // read can be retried.
      setError("Không thể cập nhật thông báo.");
    } finally {
      setReadMutationIds((previous) => { const next = new Set(previous); next.delete(id); return next; });
    }
  }, [items]);
  const markAllRead = useCallback(async () => {
    try {
      await request("/api/sales/notifications/read-all", { method: "POST", body: JSON.stringify({ through: new Date().toISOString() }) });
      setItems((previous) => previous.map((item) => ({ ...item, read_at: item.read_at ?? new Date().toISOString() })));
      setLiveUnreadIds(new Set());
      setServerUnreadCount(0);
      setError(null);
    } catch {
      // Same no-throw contract as markRead: callers use `void`, so surface
      // the failure instead of rejecting.
      setError("Không thể cập nhật thông báo.");
    }
  }, []);
  const unreadCount = serverUnreadCount + liveUnreadIds.size;
  const value = useMemo(() => ({ items, unreadCount, error, markRead, markAllRead }), [items, unreadCount, error, markRead, markAllRead]);
  return <CrmLeadStreamProvider options={streamOptions}><NotificationContext.Provider value={role === "sales" ? value : null}>{role === "sales" ? <div style={{ position: "fixed", right: 16, bottom: 16, zIndex: 20 }}><NotificationConsentControl enabled={consent.enabled} nativePermission={consent.nativePermission} loading={consent.loading} hydrated={consent.hydrated} onChange={consent.change} /></div> : null}{children}{role === "sales" ? <div aria-live="polite" aria-atomic="true" className="sr-only">{unreadCount > 0 ? `${unreadCount} thông báo chưa đọc` : ""}</div> : null}</NotificationContext.Provider></CrmLeadStreamProvider>;
}

export function useSalesNotifications() { return useContext(NotificationContext); }
