"use client";

/**
 * Client shell for the login route: owns the post-login navigation policy
 * (admin/sales -> `next` param or /admin, viewer -> public chat). Keeps that
 * concern out of LoginForm so the form stays navigation-free.
 */
import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card, Typography } from "antd";
import { useAuth } from "@/lib/AuthProvider";
import { LoginForm } from "@/features/auth/LoginForm";
import { resolveRedirectTargetAfterLogin } from "@/features/auth/loginRedirect";

const SEARCH_PARAM_REDIRECT_TARGET = "next";

export function LoginScreen() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user } = useAuth();

  const requestedRedirectPath = searchParams.get(SEARCH_PARAM_REDIRECT_TARGET);

  useEffect(() => {
    // Fires both on "already signed in" page load and right after a successful
    // signIn, because AuthProvider updates `user` from onAuthChange.
    if (user) {
      router.replace(
        resolveRedirectTargetAfterLogin({
          role: user.role,
          requestedRedirectPath,
        })
      );
    }
  }, [user, router, requestedRedirectPath]);

  return (
    <Card style={{ width: "100%", maxWidth: 400 }}>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        Đăng nhập hệ thống
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        Dành cho quản trị viên và nhân viên kinh doanh.
      </Typography.Paragraph>
      <LoginForm />
    </Card>
  );
}
