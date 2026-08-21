"use client";

import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Form, Input, Modal } from "antd";
import { CheckCircleFilled, InfoCircleFilled } from "@ant-design/icons";
import { C, FS, RADIUS } from "@/lib/tokens";
import { LeadSubmitError, submitLead } from "@/lib/api";

/** sessionStorage/localStorage key holding the accepted lead id (dup guard). */
export const LEAD_ID_STORAGE_KEY = "ragre.lead_id";

/** Vietnamese mobile number, after stripping separators (spaces , . -). */
export const PHONE_PATTERN = /^(0|\+84)(3|5|7|8|9)\d{8}$/;

const PHONE_ERROR = "Số điện thoại chưa đúng. Ví dụ: 0905123456";
const CONSENT_ERROR = "Anh/chị vui lòng đồng ý để chuyên viên gọi tư vấn.";

/** Mirrors the backend normalize_phone so FE and BE validate the same string. */
export function normalizePhone(raw: string): string {
  return raw.replace(/[\s,.-]+/g, "");
}

interface LeadFormValues {
  name?: string;
  phone: string;
  consent?: boolean;
}

type LeadFormStatus = "form" | "success" | "duplicate";

export interface LeadFormProps {
  open: boolean;
  /** Chat session id (sessionStorage "ragre.session_id"), sent with the lead. */
  sessionId: string;
  /** Best-effort context note (<= 200 chars) built from the latest answer facts. */
  notePrefill?: string;
  /** ESC / overlay / close button: never blocks the chat. */
  onClose: () => void;
  /** Fired after the backend accepted the lead (parent hides the CTA chip). */
  onSuccess: (leadId: number) => void;
}

/**
 * Customer lead-capture modal (Story 5.7). Senior-first: 48px inputs, one
 * navy accent, explicit consent, no dead ends (every failure keeps the typed
 * values and offers a retry or a close).
 */
export function LeadForm({ open, sessionId, notePrefill, onClose, onSuccess }: LeadFormProps) {
  const [form] = Form.useForm<LeadFormValues>();
  const [status, setStatus] = useState<LeadFormStatus>("form");
  const [submitting, setSubmitting] = useState(false);
  const [networkError, setNetworkError] = useState<string | null>(null);
  const [willCallMinutes, setWillCallMinutes] = useState(5);

  // Re-opening always starts clean (values, errors, status).
  useEffect(() => {
    if (!open) return;
    form.resetFields();
    setStatus("form");
    setNetworkError(null);
  }, [open, form]);

  const handleFinish = async (values: LeadFormValues) => {
    setSubmitting(true);
    setNetworkError(null);
    try {
      const result = await submitLead({
        session_id: sessionId || undefined,
        name: values.name?.trim() || undefined,
        phone: normalizePhone(values.phone),
        consent: values.consent === true,
        note: notePrefill,
      });
      try {
        window.localStorage.setItem(LEAD_ID_STORAGE_KEY, String(result.lead_id));
      } catch {
        // Storage unavailable (private mode): the backend dup check still applies.
      }
      setWillCallMinutes(result.will_call_within_minutes);
      setStatus("success");
      onSuccess(result.lead_id);
    } catch (err) {
      if (err instanceof LeadSubmitError && err.kind === "duplicate") {
        setStatus("duplicate");
      } else if (err instanceof LeadSubmitError) {
        setNetworkError(err.message);
      } else {
        setNetworkError("Không gửi được yêu cầu. Vui lòng thử lại.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      centered
      width={480}
      maskClosable
      keyboard
      title={
        <span style={{ fontSize: 20, fontWeight: 700, color: C.text }}>
          Nhận tư vấn từ chuyên viên
        </span>
      }
      styles={{ body: { paddingTop: 12 } }}
    >
      {status === "success" && (
        <div style={{ textAlign: "center", padding: "12px 0 4px" }} aria-live="polite">
          <CheckCircleFilled style={{ fontSize: 56, color: C.success }} aria-hidden="true" />
          <p
            style={{
              fontSize: FS.body,
              lineHeight: FS.bodyLine,
              color: C.text,
              margin: "16px 0 20px",
            }}
          >
            Đã ghi nhận. Chuyên viên sẽ gọi lại trong vòng ~{willCallMinutes} phút, anh/chị vui
            lòng để ý máy.
          </p>
          <Button
            block
            onClick={onClose}
            style={{ height: 48, fontSize: 16, fontWeight: 600, borderRadius: RADIUS.btn }}
          >
            Đóng
          </Button>
          <p style={{ fontSize: 13, lineHeight: "20px", color: C.textMuted, margin: "12px 0 0" }}>
            Muốn đổi số? Liên hệ hotline 09xx.
          </p>
        </div>
      )}

      {status === "duplicate" && (
        <div style={{ padding: "8px 0 4px" }} aria-live="polite">
          <div
            style={{
              display: "flex",
              gap: 12,
              alignItems: "flex-start",
              background: C.warningSoft,
              borderRadius: RADIUS.small,
              padding: 16,
              marginBottom: 20,
            }}
          >
            <InfoCircleFilled style={{ fontSize: 22, color: C.warning, marginTop: 2 }} aria-hidden="true" />
            <p style={{ fontSize: 16, lineHeight: "24px", color: C.text, margin: 0 }}>
              Số này đã đăng ký, chuyên viên sẽ gọi sớm nhất.
            </p>
          </div>
          <Button
            block
            onClick={onClose}
            style={{ height: 48, fontSize: 16, fontWeight: 600, borderRadius: RADIUS.btn }}
          >
            Đóng
          </Button>
        </div>
      )}

      {status === "form" && (
        <>
          <p style={{ fontSize: 15, lineHeight: "24px", color: C.textMuted, margin: "0 0 16px" }}>
            Để lại số điện thoại, chuyên viên của The Camellia sẽ gọi tư vấn trong khoảng 5 phút
            (giờ hành chính).
          </p>
          <Form<LeadFormValues>
            form={form}
            layout="vertical"
            onFinish={handleFinish}
            requiredMark={false}
          >
            <Form.Item
              label={
                <span style={{ fontSize: 15, fontWeight: 600, color: C.text }}>Tên anh/chị</span>
              }
              name="name"
              rules={[{ max: 50, message: "Tên tối đa 50 ký tự." }]}
              style={{ marginBottom: 14 }}
            >
              <Input
                maxLength={50}
                autoComplete="name"
                placeholder="Ví dụ: Nguyễn Văn An"
                style={{ height: 48, fontSize: 17, borderRadius: RADIUS.input }}
              />
            </Form.Item>

            <Form.Item
              label={
                <span style={{ fontSize: 15, fontWeight: 600, color: C.text }}>Số điện thoại</span>
              }
              name="phone"
              style={{ marginBottom: 14 }}
              rules={[
                {
                  validator: async (_rule, value: string | undefined) => {
                    const normalized = normalizePhone(value ?? "");
                    if (!PHONE_PATTERN.test(normalized)) {
                      throw new Error(PHONE_ERROR);
                    }
                  },
                },
              ]}
            >
              <Input
                inputMode="tel"
                autoComplete="tel"
                placeholder="Ví dụ: 0905123456"
                style={{ height: 48, fontSize: 17, borderRadius: RADIUS.input }}
              />
            </Form.Item>

            <Form.Item
              name="consent"
              valuePropName="checked"
              style={{ marginBottom: 14 }}
              rules={[
                {
                  validator: async (_rule, value: boolean | undefined) => {
                    if (value !== true) {
                      throw new Error(CONSENT_ERROR);
                    }
                  },
                },
              ]}
            >
              <Checkbox style={{ fontSize: 16, lineHeight: "24px", alignItems: "flex-start" }}>
                <span style={{ fontSize: 16, lineHeight: "24px", color: C.text }}>
                  Tôi đồng ý nhận cuộc gọi tư vấn sản phẩm The Camellia. Thông tin không chia sẻ
                  cho bên thứ ba.
                </span>
              </Checkbox>
            </Form.Item>

            <div aria-live="polite">
              {networkError && (
                <Alert
                  type="error"
                  showIcon
                  message={networkError}
                  style={{ marginBottom: 12 }}
                />
              )}
            </div>

            <Button
              type="primary"
              htmlType="submit"
              block
              loading={submitting}
              style={{
                height: 52,
                fontSize: 17,
                fontWeight: 600,
                borderRadius: RADIUS.btn,
                background: C.primary,
              }}
            >
              {submitting ? "Đang kết nối..." : networkError ? "Thử lại" : "Nhận tư vấn miễn phí"}
            </Button>
          </Form>
        </>
      )}
    </Modal>
  );
}
