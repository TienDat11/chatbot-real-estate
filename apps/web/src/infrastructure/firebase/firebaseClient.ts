/**
 * Lazy singleton Firebase client (absorbed from the former src/lib/firebase.ts).
 *
 * Returns the SAME app/auth/firestore instance on every call and reuses the
 * default app across Next.js hot reloads instead of re-initializing, exactly
 * like the original file. The config comes from firebaseEnvironmentConfig.ts,
 * which fails fast when a NEXT_PUBLIC_FIREBASE_* variable is missing.
 */
import { getApp, getApps, initializeApp, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";
import { getFirestore, type Firestore } from "firebase/firestore";
import { readFirebaseEnvironmentConfig } from "./firebaseEnvironmentConfig";

let cachedFirebaseApp: FirebaseApp | null = null;
let cachedFirebaseAuth: Auth | null = null;
let cachedFirebaseFirestore: Firestore | null = null;

/** Returns the single shared app, reusing the default app on hot reload. */
export function getFirebaseApp(): FirebaseApp {
  if (cachedFirebaseApp) {
    return cachedFirebaseApp;
  }
  // Reuse the default app across hot reloads instead of re-initializing.
  const existingDefaultApp = getApps().length > 0 ? getApp() : null;
  cachedFirebaseApp =
    existingDefaultApp ?? initializeApp(readFirebaseEnvironmentConfig());
  return cachedFirebaseApp;
}

/** Returns the single shared auth instance bound to the shared app. */
export function getFirebaseAuth(): Auth {
  if (!cachedFirebaseAuth) {
    cachedFirebaseAuth = getAuth(getFirebaseApp());
  }
  return cachedFirebaseAuth;
}

/** Returns the single shared Firestore instance bound to the shared app. */
export function getFirebaseFirestore(): Firestore {
  if (!cachedFirebaseFirestore) {
    cachedFirebaseFirestore = getFirestore(getFirebaseApp());
  }
  return cachedFirebaseFirestore;
}
