/* FCM background notifications. Firebase config is public web configuration. */
importScripts("https://www.gstatic.com/firebasejs/12.18.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/12.18.0/firebase-messaging-compat.js");

const params = new URL(self.location.href).searchParams;
firebase.initializeApp({
  apiKey: params.get("apiKey"),
  authDomain: params.get("authDomain"),
  projectId: params.get("projectId"),
  storageBucket: params.get("storageBucket"),
  messagingSenderId: params.get("messagingSenderId"),
  appId: params.get("appId"),
});

firebase.messaging().onBackgroundMessage((payload) => {
  // Messages carrying a notification payload are already displayed by the
  // FCM SDK; showing one here would duplicate the toast. Data-only messages
  // have no automatic display and need a manual notification.
  if (payload.notification) return;
  const data = payload.data || {};
  self.registration.showNotification(data.title || "Thông báo", {
    body: data.body || "",
    data,
  });
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const rawTarget = data.url || data.link;
  const targetUrl = typeof rawTarget === "string" && rawTarget.startsWith("/") && !rawTarget.startsWith("//")
    ? (() => { try { const url = new URL(rawTarget, self.location.origin); return url.origin === self.location.origin ? `${url.pathname}${url.search}${url.hash}` : "/"; } catch { return "/"; } })()
    : "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      // Reuse an existing window instead of opening a second one. When the
      // window already sits on the exact target URL, focusing is enough;
      // otherwise navigate it to the safe same-origin target first.
      const alreadyThere = clientList.find((client) => "focus" in client && client.url === (() => { try { return new URL(targetUrl, self.location.origin).href; } catch { return null; } })());
      const existing = alreadyThere || clientList.find((client) => "focus" in client);
      if (existing) {
        if (!alreadyThere && targetUrl !== "/") existing.navigate(targetUrl);
        return existing.focus();
      }
      return clients.openWindow(targetUrl);
    }),
  );
});
