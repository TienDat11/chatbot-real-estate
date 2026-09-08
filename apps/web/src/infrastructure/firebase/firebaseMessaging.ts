"use client";

import { getToken, isSupported, onMessage, type MessagePayload } from "firebase/messaging";
import { getFirebaseMessaging } from "@/infrastructure/firebase/firebaseMessagingClient";
import { readFirebaseEnvironmentConfig } from "@/infrastructure/firebase/firebaseEnvironmentConfig";

// The FCM service worker is the channel that carries a push to an open tab:
// without it registered and in control of the page, `onMessage` never fires
// and no foreground toast can arrive, even with a listener attached. Keep a
// single cached registration so the client and sales surfaces (and StrictMode
// double-mounts) share one registration instead of racing duplicate ones.
let swRegistrationPromise: Promise<ServiceWorkerRegistration | null> | null = null;

function fcmServiceWorkerUrl(): string {
  const config = readFirebaseEnvironmentConfig();
  return `/firebase-messaging-sw.js?${new URLSearchParams({
    apiKey: config.apiKey,
    authDomain: config.authDomain,
    projectId: config.projectId,
    storageBucket: config.storageBucket,
    messagingSenderId: config.messagingSenderId,
    appId: config.appId,
  })}`;
}

// Register the messaging service worker when — and only when — the browser
// permission is ALREADY granted. This never prompts; it just opens the
// delivery channel. Gating on the granted permission (not on the in-app
// consent toggle or on the backend device-token POST) is what makes a user who
// previously allowed notifications receive foreground toasts on every surface.
export function ensureFcmServiceWorkerRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (typeof window === "undefined" || typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return Promise.resolve(null);
  }
  if (typeof Notification === "undefined" || Notification.permission !== "granted") {
    return Promise.resolve(null);
  }
  if (swRegistrationPromise) return swRegistrationPromise;
  swRegistrationPromise = (async () => {
    try {
      return await navigator.serviceWorker.register(fcmServiceWorkerUrl());
    } catch (error) {
      // A failed registration must not poison the cache: clear it so a later
      // subscribe (e.g. after a transient SW hiccup) can retry.
      swRegistrationPromise = null;
      // Surface the failure: a silent null here is indistinguishable from the
      // permission gate, which is why "background delivery is dead" could not be
      // diagnosed from the client alone.
      console.warn("[fcm] service worker registration failed", error);
      return null;
    }
  })();
  return swRegistrationPromise;
}

export async function registerFcmToken(options: { requestPermission?: boolean } = {}): Promise<string | null> {
  if (typeof window === "undefined" || !(await isSupported())) return null;
  const permission = options.requestPermission ? await Notification.requestPermission() : Notification.permission;
  if (permission !== "granted") return null;
  const messaging = await getFirebaseMessaging();
  if (!messaging) return null;
  const registration = await ensureFcmServiceWorkerRegistration();
  if (!registration) return null;
  try {
    return (await getToken(messaging, { vapidKey: process.env.NEXT_PUBLIC_FIREBASE_VAPID_KEY, serviceWorkerRegistration: registration })) || null;
  } catch (error) {
    // getToken rejects on a VAPID mismatch, an SW not yet in control, or a
    // blocked token service; without this the whole registration silently
    // returns null and delivery looks "off" with no trace.
    console.warn("[fcm] getToken failed", error);
    return null;
  }
}

export async function subscribeToFcmMessages(onPayload: (payload: MessagePayload) => void): Promise<() => void> {
  if (typeof window === "undefined" || !(await isSupported())) return () => {};
  // Establish the delivery channel before attaching the listener so a push
  // arriving while this surface is open is actually routed to the tab. This
  // runs on every provider mount, independent of consent-UI visibility and of
  // token-registration success.
  await ensureFcmServiceWorkerRegistration();
  const messaging = await getFirebaseMessaging();
  if (!messaging) return () => {};
  return onMessage(messaging, onPayload);
}
