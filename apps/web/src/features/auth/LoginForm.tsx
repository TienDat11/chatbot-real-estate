"use client";

/**
 * Email/password login form (story 8.3). Talks to the auth context only, so
 * the Firebase implementation can be swapped without touching this screen.
 * Tokens are never persisted by this layer — the service keeps them in
 * memory (decisions C13/C14); this component only calls signIn.
 */
import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { LockOutlined, MailOutlined } from "@ant-design/icons";
import { useAuth } from "@/lib/AuthProvider";
import { mapFirebaseAuthErrorToVietnameseMessage } from "@/features/auth/firebaseAuthErrorMessage";

interface LoginFormValues {
  email: string;
  password: string;
}

interface LoginFormProps {
  /** Fired once the signIn promise resolves; navigation lives in the parent. */
  onLoginSucceeded?: () => void;
}

interface LoginFormInternalFields {
  email?: string;
  password?: string;
}

export function LoginForm({ onLoginSucceeded }: LoginFormProps) {
  const { signIn } = useAuth();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [friendlyErrorMessage, setFriendlyErrorMessage] = useState<string | null>(null);

  const handleSubmit = async (submittedCredentials: LoginFormValues) => {
    setIsSubmitting(true);
    setFriendlyErrorMessage(null);
    try {
      await signIn(submittedCredentials.email, submittedCredentials.password);
      onLoginSucceeded?.();
    } catch (signInError) {
      setFriendlyErrorMessage(mapFirebaseAuthErrorToVietnameseMessage(signInError));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Form<LoginFormValues>
      layout="vertical"
      requiredMark={false}
      onFinish={handleSubmit}
      autoComplete="on"
    >
      {friendlyErrorMessage ? (
        <Alert
          type="error"
          showIcon
          message="Đăng nhập không thành công"
          description={friendlyErrorMessage}
          style={{ marginBottom: 16 }}
          role="alert"
        />
      ) : null}
      <Form.Item<LoginFormInternalFields>
        name="email"
        label="Email"
        rules={[
          { required: true, message: "Vui lòng nhập email." },
          { type: "email", message: "Địa chỉ email không hợp lệ." },
        ]}
      >
        <Input
          prefix={<MailOutlined />}
          type="email"
          placeholder="ten@congty.vn"
          autoComplete="email"
          autoFocus
        />
      </Form.Item>
      <Form.Item<LoginFormInternalFields>
        name="password"
        label="Mật khẩu"
        rules={[{ required: true, message: "Vui lòng nhập mật khẩu." }]}
      >
        <Input.Password
          prefix={<LockOutlined />}
          placeholder="Mật khẩu"
          autoComplete="current-password"
        />
      </Form.Item>
      <Form.Item style={{ marginBottom: 0 }}>
        <Button type="primary" htmlType="submit" block loading={isSubmitting}>
          Đăng nhập
        </Button>
      </Form.Item>
    </Form>
  );
}
