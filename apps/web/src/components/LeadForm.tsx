"use client";

import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Form, Input, Modal } from "antd";
import { CheckCircleFilled, InfoCircleFilled } from "@ant-design/icons";
import { C, FS, RADIUS } from "@/lib/tokens";
import { LeadSubmitError, submitLead } from "@/lib/api";

/** sessionStorage/localStorage key holding the accepted lead id (dup guard). */
export const LEAD_ID_STORAGE_KEY = "ragre.lead_id";

/** Vietnamese mobile number, matching api/application/services/lead_service.py. */
export const PHONE_PATTERN = /^(0|\+84)(3[2-9]|5[5-9]|7\d|8[1-9]|9\d)\d{7}$/;

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
  /** Anonymous persistent device id (localStorage, story 10.1-FE), sent along. */
  deviceId: string;
  /** Chosen active project key (required by the backend, story 10.1/G1). */
  projectKey: string;
  /** Project commercial name for the consent copy, when one is picked. */
  projectName?: string;
  /** Best-effort context note (<= 200 chars) built from the latest answer facts. */
  notePrefill?: string;
  /**
   * Server-minted anon token (secure wave §5.6) so POST /api/lead can bind
   * the one-time quota bonus to the same identity that chatted.
   */
  anonToken?: string;
  /** ESC / overlay / close button: never blocks the chat. */
  onClose: () => void;
  /**
   * Fired after the backend accepted the lead; carries quota_bonus_granted
   * (§5.5, 0 when the identity already got its one-time bonus).
   */
  onSuccess: (leadId: number, quotaBonusGranted: number) => void;
}

/**
 * Customer lead-capture modal (Story 5.7). Senior-first: 48px inputs, one
 * navy accent, explicit consent, no dead ends (every failure keeps the typed
 * values and offers a retry or a close).
 */
export function LeadForm({ open, sessionId, deviceId, projectKey, projectName, notePrefill, anonToken, onClose, onSuccess }: LeadFormProps) {
  const [form] = Form.useForm<LeadFormValues>();
  const [status, setStatus] = useState<LeadFormStatus>("form");
  const [submitting, setSubmitting] = useState(false);
  // Re-entrancy guard (ISSUE-7): a second activation racing the pending POST
  // must never fire a second lead creation — the antd loading state is only
  // cosmetic for synthetic/duplicate events.
  const submittingRef = useRef(false);
  const [networkError, setNetworkError] = useState<string | null>(null);
  const [willCallMinutes, setWillCallMinutes] = useState(5);

  // Re-opening always starts clean (values, errors, status).
  useEffect(() => {
    if (!open) return;
    form.resetFields();
    // Defer the local reset until after the modal-open commit. This avoids a
    // synchronous cascading render while preserving the clean reopen state.
    const resetId = window.setTimeout(() => {
      setStatus("form");
      setNetworkError(null);
    }, 0);
    return () => window.clearTimeout(resetId);
  }, [open, form]);

  const handleFinish = async (values: LeadFormValues) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setNetworkError(null);
    try {
      const result = await submitLead({
        project_key: projectKey,
        session_id: sessionId || undefined,
        device_id: deviceId || undefined,
        anon_token: anonToken || undefined,
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
      onSuccess(result.lead_id, result.quota_bonus_granted ?? 0);
    } catch (err) {
      if (err instanceof LeadSubmitError && err.kind === "duplicate") {
        setStatus("duplicate");
      } else if (err instanceof LeadSubmitError) {
        setNetworkError(err.message);
      } else {
        setNetworkError("Không gửi được yêu cầu. Vui lòng thử lại.");
      }
    } finally {
      submittingRef.current = false;
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
            Để lại số điện thoại, chuyên viên của {projectName ?? "dự án"} sẽ gọi tư vấn trong khoảng 5 phút
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
                  Tôi đồng ý nhận cuộc gọi tư vấn sản phẩm {projectName ?? "dự án"}. Thông tin không chia sẻ
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

            {/* Residual issue (dismissal contract): every form state carries an
                explicit Đóng control next to the primary action, so dismissal
                never depends on spotting the small X or pressing ESC. */}
            <div style={{ display: "flex", gap: 10 }}>
              <Button
                onClick={onClose}
                disabled={submitting}
                style={{
                  height: 52,
                  fontSize: 16,
                  fontWeight: 600,
                  borderRadius: RADIUS.btn,
                  flexShrink: 0,
                }}
              >
                Đóng
              </Button>
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
            </div>
          </Form>
        </>
      )}
    </Modal>
  );
}
