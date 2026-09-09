import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { getAnonToken } from "@/features/chat/identity";

/** BE endpoint that binds the device's anon identity to the signed-in account. */
export const LINK_ANON_ENDPOINT = "/api/auth/link-anon";

/** Bounded so a hung backend can never delay the login flow. */
const LINK_ANON_TIMEOUT_MS = 10_000;

/**
 * Best-effort link of the device's persisted anon identity (localStorage
 * `ragre.anon_token`) to the freshly signed-in Firebase account
 * (POST /api/auth/link-anon, bearer = fresh ID token).
 *
 * Multi-device model: every device keeps its OWN anon token; each links into
 * the shared account identity. Idempotent on the backend — relinking the same
 * (device, account) pair succeeds without re-merging quota — so firing this
 * once per sign-in is safe and cheap.
 *
 * NEVER rejects and NEVER mutates app state: any failure (no stored token,
 * staff role, 401/409/422/5xx, network, timeout) is logged and swallowed so
 * the login flow continues exactly as before. Staff tokens cannot be linked
 * (the backend 403s them); the caller filters, and this helper double-checks.
 */
export async function linkAnonIdentityAfterLogin(
  user: { uid: string; role: string | null },
  storage: Storage = window.localStorage
): Promise<boolean> {
  if ((user.role ?? "viewer") === "sales" || user.role === "admin") {
    return false;
  }
  let anonToken: string | null;
  try {
    anonToken = getAnonToken(storage);
  } catch {
    return false;
  }
  if (!anonToken) {
    return false;
  }
  try {
    const response = await fetch(LINK_ANON_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${await getFreshIdToken()}`,
      },
      body: JSON.stringify({ anon_token: anonToken }),
      signal: AbortSignal.timeout(LINK_ANON_TIMEOUT_MS),
    });
    if (!response.ok) {
      console.warn(
        `[identity] link-anon not applied (${response.status}) for ${user.uid}; continuing`
      );
      return false;
    }
    return true;
  } catch (error) {
    console.warn(`[identity] link-anon failed for ${user.uid}; continuing`, error);
    return false;
  }
}
