import type { ReactNode } from "react";
import { App as AntdApp } from "antd";
import { AuthProvider } from "@/lib/AuthProvider";
import { RequireRole } from "@/components/RequireRole";
import { AppShell } from "@/components/AppShell";
import { RealtimeProvider } from "@/lib/realtime/RealtimeProvider";
import { SalesNotificationProvider } from "@/features/notifications/SalesNotificationProvider";

export default function SalesLayout({ children }: { children: ReactNode }) {
  return <AuthProvider><RequireRole allowedRoles={["admin", "sales"]}><AntdApp><RealtimeProvider><SalesNotificationProvider><AppShell>{children}</AppShell></SalesNotificationProvider></RealtimeProvider></AntdApp></RequireRole></AuthProvider>;
}
