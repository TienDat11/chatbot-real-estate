/**
 * Post-login redirect resolution (story 8.3 / ISSUE-5). Role-split: admin
 * keeps the shared project-home default, sales lands on the CRM lead board,
 * and viewers always land on the public chat. A staff `next` path is honoured
 * only when it is a safe same-origin internal path and never loops back to the
 * login route itself. Pure function so the policy is unit-testable.
 */
import type { Role } from "@/domain/auth/role";

/** Where an admin user lands when no valid `next` param was supplied. */
export const ADMIN_REDIRECT_PATH = "/project/camellia";

/** Where a sales user lands when no valid `next` param was supplied. */
export const SALES_REDIRECT_PATH = "/sales/leads";

/** Where customer users land after login. */
export const VIEWER_REDIRECT_PATH = "/project/camellia";

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

/**
 * Loop guard: honouring `/login` as a redirect target would bounce straight
 * back into this resolver after sign-in, so it is rejected for every role.
 */
function isLoginRoutePath(path: string): boolean {
  // Strip query/hash so /login?next=/sales/leads is still recognised.
  return path.split(/[?#]/, 1)[0] === "/login";
}

/**
 * Loop guard (residual audit): the bare root always renders the login/guest
 * auth shell, so honouring "/" sends a just-signed-in user straight back into
 * auth UI instead of their role workspace. Rejected for every role; the role
 * default applies instead.
 */
function isAuthShellPath(path: string): boolean {
  return path.split(/[?#]/, 1)[0] === "/";
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
  const hasValidNext =
    requestedRedirectPath !== null &&
    isSafeInternalPath(requestedRedirectPath) &&
    !isLoginRoutePath(requestedRedirectPath) &&
    !isAuthShellPath(requestedRedirectPath);
  if (hasValidNext) {
    return requestedRedirectPath;
  }
  return role === "sales" ? SALES_REDIRECT_PATH : ADMIN_REDIRECT_PATH;
}
