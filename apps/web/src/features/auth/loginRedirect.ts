/**
 * Post-login redirect resolution (story 8.3). Admin/sales users go to the
 * requested `next` path (default /admin); viewer users always land on the
 * public chat. Pure function so the policy is unit-testable.
 */
import type { Role } from "@/domain/auth/role";

/** Where an admin/sales user lands when no `next` param was supplied. */
export const DEFAULT_ADMIN_REDIRECT_PATH = "/admin";

/** Where viewer-role users always land after login. */
export const VIEWER_REDIRECT_PATH = "/";

interface ResolveRedirectTargetInput {
  role: Role | null;
  requestedRedirectPath: string | null;
}

/**
 * Only same-origin app paths are honoured; absolute URLs and protocol-relative
 * URLs are rejected to block open-redirect abuse of the `next` parameter.
 */
function isSafeInternalPath(path: string): boolean {
  return path.startsWith("/") && !path.startsWith("//") && !path.includes("\\");
}

/** Resolves the final post-login destination for the signed-in role. */
export function resolveRedirectTargetAfterLogin({
  role,
  requestedRedirectPath,
}: ResolveRedirectTargetInput): string {
  // A missing role claim is treated as least privilege: public chat only.
  if (role !== "admin" && role !== "sales") {
    return VIEWER_REDIRECT_PATH;
  }
  if (requestedRedirectPath && isSafeInternalPath(requestedRedirectPath)) {
    return requestedRedirectPath;
  }
  return DEFAULT_ADMIN_REDIRECT_PATH;
}
