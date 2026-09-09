"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { App } from "antd";
import { useAuth } from "@/lib/AuthProvider";
import { subscribeToFcmMessages } from "@/infrastructure/firebase/firebaseMessaging";
import { sameOriginNotificationPath } from "@/infrastructure/firebase/firebaseNotificationUtils";
import { NotificationConsentControl } from "@/features/notifications/NotificationConsentControl";
import { useFcmConsent } from "@/features/notifications/useFcmConsent";

// Foreground payload contract for the client journey: only call-started
// events surface a toast; anything else (new_lead is sales-only, unknown or
// malformed types included) is ignored so junk payloads stay silent.
function isClientCallStarted(payload: { data?: Record<string, unknown> }): boolean {
  return payload.data?.type === "sales_call_started";
}

export function ClientNotificationProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { notification } = App.useApp();
  const pathname = usePathname();
  const isClient = pathname.startsWith("/project/") && !auth.loading;
  // Scope FCM consent/token by the signed-in Firebase UID (null for an
  // anonymous visitor) so a logout/login cannot carry one account's state to
  // the next. See useFcmConsent for the keying and epoch guarantees.
  const uid = auth.user?.uid ?? null;
  const consent = useFcmConsent("client", isClient, uid);
  // The message listener is mounted for the whole provider lifetime, not
  // gated on pathname/consent visibility: an authorized foreground event can
  // arrive on any surface ("visitor on / when the call starts"), and a
  // pathname-keyed effect would tear down and re-subscribe (duplicate
  // listeners, dropped events) on every navigation or auth refresh. Token
  // registration stays consent-driven inside useFcmConsent; receiving an
  // already-authorized foreground message never requires it to succeed.
  // `notification` from antd's App.useApp() is a stable instance, and the
  // handler closes only over it, so the effect runs once per mount.
  const notificationRef = useRef(notification);
  useEffect(() => {
    notificationRef.current = notification;
  }, [notification]);
  useEffect(() => {
    let cancelled = false;
    let unsubscribe: () => void = () => {};
    // Subscribe resolves asynchronously; if the component unmounts first,
    // the resolved unsubscribe must still run so no orphan Firebase
    // listener survives StrictMode double-mounts or provider swaps.
    void subscribeToFcmMessages((payload) => {
      if (!isClientCallStarted(payload)) return;
      const title = payload.notification?.title ?? "Cuộc gọi bắt đầu";
      const body = payload.notification?.body ?? "Chuyên viên đang gọi cho bạn.";
      notificationRef.current.open({
        message: title,
        description: body,
        duration: 5,
        onClick: () => { window.location.assign(sameOriginNotificationPath(payload.data?.url)); },
      });
    }).then((stop) => { if (cancelled) stop(); else unsubscribe = stop; });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);
  // The consent control only exists on a project conversation surface: FCM
  // consent is meaningless elsewhere and the control would float over pages
  // that never receive client notifications. Only the UI is route-specific;
  // the listener above is not.
  return <>{isClient ? <div style={{ position: "fixed", right: 16, bottom: 16, zIndex: 20 }}><NotificationConsentControl enabled={consent.enabled} nativePermission={consent.nativePermission} loading={consent.loading} hydrated={consent.hydrated} onChange={consent.change} /></div> : null}{children}</>;
}
