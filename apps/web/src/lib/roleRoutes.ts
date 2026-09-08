import type { Role } from "@/domain/auth/role";

export interface RoleRoute {
  href: string;
  label: string;
  roles: readonly Role[];
}

/** Canonical destinations exposed by the shared application navigation. */
export const APP_ROUTES: readonly RoleRoute[] = [
  { href: "/sales/leads", label: "CRM", roles: ["admin", "sales"] },
  { href: "/sales/chat", label: "Chat bán hàng", roles: ["admin", "sales"] },
  { href: "/sales/train", label: "Đào tạo", roles: ["admin", "sales"] },
  { href: "/admin", label: "Quản trị", roles: ["admin"] },
];

export function routesForRole(role: Role | null): readonly RoleRoute[] {
  return APP_ROUTES.filter((route) => role !== null && route.roles.includes(role));
}
