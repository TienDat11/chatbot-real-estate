/**
 * Role-gating decision for the RequireRole guard. Extracted as a pure tri-state
 * so the allow/deny/loading matrix is unit-testable without rendering.
 */
import type { Role } from "@/domain/auth/role";

/** What RequireRole should render for the current auth snapshot. */
export type RoleAccessDecision = "loading" | "allowed" | "denied";

interface EvaluateRoleAccessInput {
  isLoading: boolean;
  role: Role | null;
  allowedRoles: readonly Role[];
}

/** True when the current role is explicitly listed in the allow-list. */
export function isRoleAllowed(role: Role | null, allowedRoles: readonly Role[]): boolean {
  return role !== null && allowedRoles.includes(role);
}

/**
 * Loading always wins so the guard never flashes a 403 at a user whose claims
 * are still being fetched; denial covers signed-out and unlisted roles.
 */
export function evaluateRoleAccess({
  isLoading,
  role,
  allowedRoles,
}: EvaluateRoleAccessInput): RoleAccessDecision {
  if (isLoading) {
    return "loading";
  }
  return isRoleAllowed(role, allowedRoles) ? "allowed" : "denied";
}
