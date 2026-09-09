"use client";

import { useEffect } from "react";
import { Spin, Typography } from "antd";
import { useRouter } from "next/navigation";
import { LoginScreen as AuthLoginScreen } from "@/features/auth/LoginScreen";
import { resolveRedirectTargetAfterLogin } from "@/features/auth/loginRedirect";
import { useAuth } from "@/lib/AuthProvider";

/**
 * Holds the bare root on a neutral loading frame until Firebase resolves the
 * session, then sends authenticated users to the shared role destination.
 */
export function RootAuthGate() {
  const router = useRouter();
  const { user, loading } = useAuth();

  useEffect(() => {
    if (!loading && user) {
      router.replace(
        resolveRedirectTargetAfterLogin({
          role: user.role,
          requestedRedirectPath: null,
        }),
      );
    }
  }, [loading, router, user]);

  if (loading || user) {
    return (
      <div
        className="app-viewport-min-height"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 12,
        }}
        role="status"
        aria-live="polite"
      >
        <Spin size="large" />
        <Typography.Text type="secondary">Đang kiểm tra phiên đăng nhập...</Typography.Text>
      </div>
    );
  }

  return <AuthLoginScreen />;
}
