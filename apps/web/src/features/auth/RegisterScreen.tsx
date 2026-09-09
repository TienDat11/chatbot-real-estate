"use client";

import { useEffect, useState } from "react";
import { Alert, Button, Form, Input, Typography } from "antd";
import { LockOutlined, MailOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthProvider";
import { mapFirebaseAuthErrorToVietnameseMessage } from "@/features/auth/firebaseAuthErrorMessage";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

interface RegistrationValues {
  email: string;
  password: string;
  confirmPassword: string;
}

/** Customer-only registration; staff accounts remain provisioned by administrators. */
export function RegisterScreen() {
  const router = useRouter();
  const { user, signUp } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (user) router.replace("/");
  }, [router, user]);

  async function handleSubmit(values: RegistrationValues): Promise<void> {
    setSubmitting(true);
    setError(null);
    try {
      await signUp(values.email, values.password);
      router.replace("/");
    } catch (registrationError) {
      setError(mapFirebaseAuthErrorToVietnameseMessage(registrationError));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main
      className="app-viewport-min-height"
      style={{
        display: "grid",
        placeItems: "center",
        padding: 24,
        background:
          "radial-gradient(1000px 480px at 15% -10%, rgba(201, 162, 75, 0.18), transparent 60%)," +
          "radial-gradient(900px 420px at 90% 110%, rgba(168, 80, 46, 0.16), transparent 55%)," +
          "linear-gradient(135deg, #0E2A47 0%, #141D2B 55%, #1B2737 100%)",
      }}
    >
      <section style={{ width: "100%", maxWidth: 440, background: C.surface, border: `1px solid ${C.border}`, borderRadius: RADIUS.card, padding: 32, boxShadow: SHADOW.pop }}>
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
        <Typography.Text className="eyebrow" style={{ display: "block", textAlign: "center", color: C.terracotta }}>Tài khoản khách hàng</Typography.Text>
        <Typography.Title level={3} style={{ marginTop: 8 }}>Tạo tài khoản</Typography.Title>
        <Typography.Paragraph type="secondary">Đăng ký để lưu lịch sử tư vấn và tiếp tục cuộc trò chuyện trên các thiết bị.</Typography.Paragraph>
        <Form<RegistrationValues> layout="vertical" requiredMark={false} onFinish={handleSubmit} autoComplete="on">
          {error ? <Alert role="alert" type="error" showIcon message="Đăng ký không thành công" description={error} style={{ marginBottom: 16 }} /> : null}
          <Form.Item name="email" label="Email" rules={[{ required: true, message: "Vui lòng nhập email." }, { type: "email", message: "Địa chỉ email không hợp lệ." }]}>
            <Input prefix={<MailOutlined />} type="email" autoComplete="email" />
          </Form.Item>
          <Form.Item name="password" label="Mật khẩu" rules={[{ required: true, min: 8, message: "Mật khẩu cần ít nhất 8 ký tự." }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="confirmPassword" label="Nhập lại mật khẩu" dependencies={["password"]} rules={[{ required: true, message: "Vui lòng nhập lại mật khẩu." }, ({ getFieldValue }) => ({ validator(_, value) { return !value || getFieldValue("password") === value ? Promise.resolve() : Promise.reject(new Error("Mật khẩu nhập lại không khớp.")); } })]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>Đăng ký tài khoản</Button>
        </Form>
        <Button type="link" block onClick={() => router.push("/login")}>Đã có tài khoản? Đăng nhập</Button>
      </section>
    </main>
  );
}
