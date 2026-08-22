/**
 * Domain-level authorization roles, sourced from Firebase custom claims.
 *
 * Lives in domain so both infrastructure (claim decoding) and UI (route
 * guards) depend on this module, never on each other.
 */

/** Roles assigned via Firebase custom claims; unknown claims degrade to viewer. */
export type Role = "admin" | "sales" | "viewer";

/** Every role literal, in descending privilege order. */
export const ALL_ROLES: readonly Role[] = ["admin", "sales", "viewer"];

/**
 * Safely narrows an arbitrary custom-claim value into a Role. Anything the
 * claims admin did not set explicitly (undefined, typos, future roles) falls
 * back to the least-privileged viewer so a bad claim can never widen access.
 */
export function parseRoleFromClaim(claimValue: unknown): Role {
  return ALL_ROLES.includes(claimValue as Role) ? (claimValue as Role) : "viewer";
}
