"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { Layout, Space, Tag, Typography } from "antd";
import { HomeOutlined, ReadOutlined, SafetyCertificateOutlined, TeamOutlined, MessageOutlined } from "@ant-design/icons";
import { AccountControls } from "@/components/AccountControls";
import { NotificationCenter } from "@/features/notifications/NotificationCenter";
import { useOptionalAuth } from "@/lib/AuthProvider";
import { routesForRole } from "@/lib/roleRoutes";
import { C, RADIUS } from "@/lib/tokens";

const iconByRoute: Record<string, ReactNode> = {
  "/": <HomeOutlined aria-hidden="true" />,
  "/sales/leads": <TeamOutlined aria-hidden="true" />,
  "/sales/chat": <MessageOutlined aria-hidden="true" />,
  "/sales/train": <ReadOutlined aria-hidden="true" />,
  "/admin": <SafetyCertificateOutlined aria-hidden="true" />,
};

interface AppShellProps {
  children: ReactNode;
  title?: string;
  subtitle?: string;
  contentClassName?: string;
}

/** Single light application frame shared by authenticated workspaces. */
export function AppShell({ children, title, subtitle, contentClassName }: AppShellProps) {
  const auth = useOptionalAuth();
  const routes = routesForRole(auth?.user?.role ?? null);

  return (
    <Layout className="app-shell" style={{ minHeight: "100dvh", background: C.bg }}>
      <header className="app-shell__header" style={{ background: C.surface, borderBottom: `1px solid ${C.border}` }}>
        <div className="app-shell__header-inner">
          <Link href="/" className="app-shell__brand" aria-label="RAG Real Estate, về trang tra cứu">
            <span className="app-shell__mark" aria-hidden="true">R</span>
            <span>
              <Typography.Text strong style={{ color: C.primary, display: "block" }}>RAG Real Estate</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>Tra cứu pháp lý</Typography.Text>
            </span>
          </Link>
          <nav aria-label="Điều hướng chính" className="app-shell__nav">
            {routes.map((route) => (
              <Link key={route.href} href={route.href} className="app-shell__nav-link">
                {iconByRoute[route.href]}
                <span>{route.label}</span>
              </Link>
            ))}
          </nav>
          <Space size={12} className="app-shell__account">
            {auth?.user?.role ? <Tag color="blue" style={{ margin: 0, borderRadius: RADIUS.pill }}>{auth.user.role}</Tag> : null}
            {auth?.user?.role === "sales" ? <NotificationCenter /> : null}
            <AccountControls />
          </Space>
        </div>
      </header>
      <main className={`app-shell__content ${contentClassName ?? ""}`}>
        {title ? <div className="app-shell__intro">
          <Typography.Title level={2} style={{ margin: 0, color: C.text }}>{title}</Typography.Title>
          {subtitle ? <Typography.Paragraph type="secondary" style={{ margin: "6px 0 0" }}>{subtitle}</Typography.Paragraph> : null}
        </div> : null}
        {children}
      </main>
    </Layout>
  );
}
