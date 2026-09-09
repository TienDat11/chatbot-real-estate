import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";

/**
 * Token provider for lib/api's /query bearer seam. Resolves the signed-in
 * user's Firebase ID token FRESH per request (getIdToken caches internally
 * and auto-refreshes expired tokens, so per-send calls stay cheap while never
 * shipping a stale credential). Resolves null when nobody is signed in or
 * Firebase is unavailable — anonymous callers keep the anon_token flow
 * unchanged and no Authorization header is sent.
 */
export async function firebaseQueryAuthToken(): Promise<string | null> {
  try {
    return await getFreshIdToken();
  } catch {
    return null;
  }
}
