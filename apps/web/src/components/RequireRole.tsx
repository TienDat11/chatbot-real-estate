"use client";

/**
 * Route guard (story 8.3): renders children only when the signed-in user's
 * role is allow-listed. While claims are still loading it shows a spinner —
 * never a flash of 403 — and denied users get an antd Result with a way back
 * to the login screen.
 */
import type { ReactNode } from "react";
import { Button, Result, Spin } from "antd";
import { useAuth } from "@/lib/AuthProvider";
import { evaluateRoleAccess } from "@/features/auth/roleAccess";
import type { Role } from "@/domain/auth/role";

interface RequireRoleProps {
  allowedRoles: Role[];
  children: ReactNode;
}

export function RequireRole({ allowedRoles, children }: RequireRoleProps) {
  const { user, loading } = useAuth();
  const accessDecision = evaluateRoleAccess({
    isLoading: loading,
    role: user?.role ?? null,
    allowedRoles,
  });

  if (accessDecision === "loading") {
    return (
      <div
        style={{
          minHeight: "50vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
        aria-busy="true"
        aria-label="Đang tải thông tin đăng nhập"
      >
        <Spin size="large" />
      </div>
    );
  }

  if (accessDecision === "denied") {
    return (
      <Result
        status="403"
        title="403"
        subTitle="Bạn không có quyền truy cập trang này. Vui lòng đăng nhập bằng tài khoản được cấp quyền."
        extra={
          <Button type="primary" href="/login">
            Đến trang đăng nhập
          </Button>
        }
      />
    );
  }

  return <>{children}</>;
}
