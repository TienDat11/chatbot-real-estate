"use client";

import { Button, Space, Typography } from "antd";
import { LoginOutlined, LogoutOutlined, UserOutlined } from "@ant-design/icons";
import { useRouter } from "next/navigation";
import { useOptionalAuth } from "@/lib/AuthProvider";
import { RADIUS } from "@/lib/tokens";

/** Shared account actions for customer, staff, and chat surfaces. */
export function AccountControls() {
  const auth = useOptionalAuth();
  const router = useRouter();
  const currentUrl = typeof window === "undefined"
    ? "/"
    : `${window.location.pathname}${window.location.search}`;
  const loginHref = `/login?next=${encodeURIComponent(currentUrl)}`;

  if (auth?.loading) return null;

  if (!auth?.user) {
    return (
      <Button
        type="link"
        href={loginHref}
        icon={<LoginOutlined />}
        aria-label="Mở trang đăng nhập"
        style={{ height: 40, fontWeight: 600, paddingInline: 8 }}
      >
        Đăng nhập
      </Button>
    );
  }

  const label = auth.user.displayName || auth.user.email || "Tài khoản";
  return (
    <Space size={8} wrap>
      <Typography.Text
        type="secondary"
        style={{ maxWidth: 220, display: "inline-flex", alignItems: "center", gap: 6 }}
        title={label}
      >
        <UserOutlined aria-hidden="true" />
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {label}
        </span>
      </Typography.Text>
      <Button
        type="default"
        icon={<LogoutOutlined />}
        aria-label="Đăng xuất tài khoản"
        onClick={() => {
          void auth.signOut().then(() => router.push(loginHref));
        }}
        style={{ height: 40, fontWeight: 600, borderRadius: RADIUS.btn }}
      >
        Đăng xuất
      </Button>
    </Space>
  );
}
