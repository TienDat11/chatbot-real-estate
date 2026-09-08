"use client";

/**
 * Client shell for the login route: owns the post-login navigation policy
 * (admin/sales -> `next` param or /admin, viewer -> public chat). Keeps that
 * concern out of LoginForm so the form stays navigation-free.
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import Image from "next/image";
import { useRouter, useSearchParams } from "next/navigation";
import { Button, Typography } from "antd";
import { ArrowRightOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { useAuth } from "@/lib/AuthProvider";
import { LoginForm } from "@/features/auth/LoginForm";
import { RootProjectGate } from "@/components/RootProjectGate";
import { resolveRedirectTargetAfterLogin } from "@/features/auth/loginRedirect";
import { GREETING_IMAGES } from "@/lib/greetingContent";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

const SEARCH_PARAM_REDIRECT_TARGET = "next";

function subscribeToClientReady() {
  return () => undefined;
}

function getClientReadySnapshot() {
  return true;
}

function getServerReadySnapshot() {
  return false;
}

export function LoginScreen() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user } = useAuth();
  const [guestMode, setGuestMode] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [loginSubmitted, setLoginSubmitted] = useState(false);
  // The server-rendered CTA can be visible before React hydrates its click
  // handler. Keep it disabled until the client snapshot is active so a first
  // visit cannot lose the click in that window.
  const clientReady = useSyncExternalStore(
    subscribeToClientReady,
    getClientReadySnapshot,
    getServerReadySnapshot,
  );

  const requestedRedirectPath = searchParams.get(SEARCH_PARAM_REDIRECT_TARGET);

  useEffect(() => {
    // Fires both on "already signed in" page load and right after a successful
    // signIn, because AuthProvider updates `user` from onAuthChange.
    // An existing Firebase session must remain intact while this page is open.
    // Redirect only after this screen's form intentionally completes sign-in;
    // this makes re-authentication reachable without a redirect loop.
    if (user && loginSubmitted) {
      router.replace(
        resolveRedirectTargetAfterLogin({
          role: user.role,
          requestedRedirectPath,
        })
      );
    }
  }, [user, loginSubmitted, router, requestedRedirectPath]);

  if (guestMode) {
    return <RootProjectGate />;
  }

  return (
    <div
      className="card-in"
      style={{
        width: "100%",
        maxWidth: 980,
        display: "grid",
        gridTemplateColumns: "3fr 2fr",
        background: C.surface,
        borderRadius: 20,
        overflow: "hidden",
        boxShadow: SHADOW.pop,
        border: "1px solid " + C.border,
      }}
    >
      <div
        className="login-visual auth-visual"
        style={{
          minHeight: 520,
          padding: 36,
          display: "flex",
          alignItems: "flex-end",
          color: "#fff",
          position: "relative",
          backgroundImage: imageFailed
            ? undefined
            : `linear-gradient(180deg, rgba(14,42,71,.08), rgba(14,42,71,.9)), url(${GREETING_IMAGES[1]?.url_cdn ?? ""})`,
          backgroundSize: "cover",
          backgroundPosition: "center",
        }}
      >
        {!imageFailed && GREETING_IMAGES[1]?.url_cdn ? (
          <Image
            src={GREETING_IMAGES[1].url_cdn}
            alt="Phối cảnh dự án The Camellia Sơn Trà"
            onError={() => setImageFailed(true)}
            aria-hidden="true"
            fill
            sizes="(max-width: 900px) 100vw, 60vw"
            unoptimized
            style={{ objectFit: "cover", opacity: 0, pointerEvents: "none" }}
          />
        ) : null}
        <div>
          <Typography.Title level={2} style={{ color: "#fff", marginBottom: 8 }}>Tư vấn dự án sáng rõ hơn.</Typography.Title>
          <Typography.Text style={{ color: "rgba(255,255,255,.94)" }}>Khám phá thông tin dự án, pháp lý và chính sách bán hàng trong một không gian riêng tư.</Typography.Text>
        </div>
      </div>
      <div style={{ padding: "32px 32px 28px" }}>
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
      <LoginForm onLoginSucceeded={() => setLoginSubmitted(true)} />
      {user ? (
        <Typography.Paragraph type="secondary" style={{ textAlign: "center", marginBottom: 0 }}>
          Bạn đang đăng nhập. Nhập lại thông tin để xác thực tài khoản hiện tại.
        </Typography.Paragraph>
      ) : null}
      <Button type="link" block onClick={() => router.push("/register")} style={{ marginTop: 8, minHeight: 44 }}>
        Khách hàng mới? Tạo tài khoản
      </Button>
      <Button
        type="primary"
        size="large"
        block
        icon={<ArrowRightOutlined />}
        onClick={() => setGuestMode(true)}
        disabled={!clientReady}
        data-testid="guest-entry"
        style={{ marginTop: 12, minHeight: 48, fontWeight: 600 }}
      >
        Trải nghiệm chatbot trước khi đăng ký
      </Button>
      </div>
    </div>
  );
}
