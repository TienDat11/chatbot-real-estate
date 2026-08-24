"use client";

/**
 * Client shell for the login route: owns the post-login navigation policy
 * (admin/sales -> `next` param or /admin, viewer -> public chat). Keeps that
 * concern out of LoginForm so the form stays navigation-free.
 */
import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Typography } from "antd";
import { SafetyCertificateOutlined } from "@ant-design/icons";
import { useAuth } from "@/lib/AuthProvider";
import { LoginForm } from "@/features/auth/LoginForm";
import { resolveRedirectTargetAfterLogin } from "@/features/auth/loginRedirect";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

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
    <div
      className="card-in"
      style={{
        width: "100%",
        maxWidth: 420,
        background: C.surface,
        borderRadius: 20,
        padding: "32px 32px 28px",
        boxShadow: SHADOW.pop,
        border: "1px solid " + C.border,
      }}
    >
      <div
        className="hero-badge"
        style={{
          width: 56,
          height: 56,
          margin: "0 auto 16px",
          borderRadius: RADIUS.card,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 26,
        }}
      >
        <SafetyCertificateOutlined />
      </div>
      <Typography.Text
        className="eyebrow"
        style={{ display: "block", textAlign: "center", color: C.terracotta, marginBottom: 8 }}
      >
        Nền tảng tư vấn bất động sản
      </Typography.Text>
      <Typography.Title level={4} style={{ marginTop: 0, textAlign: "center", color: C.text }}>
        Đăng nhập hệ thống
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ textAlign: "center" }}>
        Dành cho quản trị viên và nhân viên kinh doanh.
      </Typography.Paragraph>
      <LoginForm />
    </div>
  );
}
