"use client";

import { getMessaging, isSupported, type Messaging } from "firebase/messaging";
import { getFirebaseApp } from "@/infrastructure/firebase/firebaseClient";

let messaging: Messaging | null | undefined;
export async function getFirebaseMessaging(): Promise<Messaging | null> {
  if (messaging !== undefined) return messaging;
  messaging = (await isSupported()) ? getMessaging(getFirebaseApp()) : null;
  return messaging;
}
