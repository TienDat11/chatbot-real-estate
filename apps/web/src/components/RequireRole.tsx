"use client";

/**
 * Route guard (story 8.3): renders children only when the signed-in user's
 * role is allow-listed. While claims are still loading it shows a spinner —
 * never a flash of 403 — signed-out visitors are redirected to /login with a
 * next= deep link back to the original path+query, and authenticated users
 * lacking the role stay fail-closed on a 403 Result.
 */
import type { ReactNode } from "react";
import { Button, Result, Spin } from "antd";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { useAuth } from "@/lib/AuthProvider";
import { evaluateRoleAccess } from "@/features/auth/roleAccess";
import type { Role } from "@/domain/auth/role";

interface RequireRoleProps {
  allowedRoles: Role[];
  children: ReactNode;
}

/**
 * Public guard shell. The Suspense boundary must sit above the
 * useSearchParams() consumer (RequireRoleGate) so static prerender of gated
 * pages (e.g. /admin) bails out to the spinner instead of failing the build
 * with the missing-suspense deopt.
 */
export function RequireRole({ allowedRoles, children }: RequireRoleProps) {
  return (
    <Suspense fallback={<AccessChecking />}>
      <RequireRoleGate allowedRoles={allowedRoles}>{children}</RequireRoleGate>
    </Suspense>
  );
}

function RequireRoleGate({ allowedRoles, children }: RequireRoleProps) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const accessDecision = evaluateRoleAccess({
    isLoading: loading,
    role: user?.role ?? null,
    allowedRoles,
  });

  useEffect(() => {
    if (accessDecision === "denied" && !user && pathname !== "/login") {
      const next = `${pathname}${searchParams.toString() ? `?${searchParams.toString()}` : ""}`;
      router.replace(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [accessDecision, pathname, router, searchParams, user]);

  if (accessDecision === "loading") {
    return <AccessChecking />;
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

/** Spinner shell shared by the auth-loading state and the prerender fallback. */
function AccessChecking() {
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
