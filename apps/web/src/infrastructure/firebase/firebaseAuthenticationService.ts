/**
 * Firebase email/password authentication service (absorbed from the former
 * src/lib/auth.ts, same exported function names).
 *
 * Sits in infrastructure because it is the ONLY layer allowed to touch
 * firebase/auth. It exposes a narrowed AuthenticatedUser view so the React
 * layer (AuthProvider) never imports a firebase type directly.
 */
import {
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
} from "firebase/auth";
import { getFirebaseAuth } from "./firebaseClient";

/**
 * Narrow, serializable view of the authenticated Firebase user. Keeps firebase
 * types out of src/lib and makes the auth contract transport-agnostic.
 */
export interface AuthenticatedUser {
  uid: string;
  email: string | null;
  displayName: string | null;
  photoURL: string | null;
}

/** Creates a new account with email/password. */
export function signUpWithEmail(email: string, password: string) {
  return createUserWithEmailAndPassword(getFirebaseAuth(), email, password);
}

/** Signs in an existing account with email/password. */
export function signInWithEmail(email: string, password: string) {
  return signInWithEmailAndPassword(getFirebaseAuth(), email, password);
}

/** Signs out the current user. */
export function signOutUser() {
  return signOut(getFirebaseAuth());
}

/** Subscribes to auth-state changes; returns the unsubscribe function. */
export function onAuthChange(callback: (user: AuthenticatedUser | null) => void) {
  return onAuthStateChanged(getFirebaseAuth(), (firebaseUser) => {
    callback(
      firebaseUser
        ? {
            uid: firebaseUser.uid,
            email: firebaseUser.email,
            displayName: firebaseUser.displayName,
            photoURL: firebaseUser.photoURL,
          }
        : null
    );
  });
}
