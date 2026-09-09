"use client";

import type { ReactNode } from "react";
import { App as AntdApp } from "antd";
import { AuthProvider } from "@/lib/AuthProvider";
import { ClientNotificationProvider } from "@/features/notifications/ClientNotificationProvider";

export function ClientNotificationShell({ children }: { children: ReactNode }) {
  return <AuthProvider><AntdApp><ClientNotificationProvider>{children}</ClientNotificationProvider></AntdApp></AuthProvider>;
}
