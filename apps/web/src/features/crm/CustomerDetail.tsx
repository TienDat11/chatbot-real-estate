"use client";

/**
 * CustomerDetail — CRM customer drawer (story 9.3).
 *
 * Three persistence paths, deliberately split by ownership:
 *  - note editor -> CrmNoteStorePort (FIRESTORE-NATIVE: written by this staff
 *    client straight to notes/{leadId}, never mirrored back);
 *  - status/rejection/reengage + revealed phone + consent withdrawal ->
 *    crmApiClient (backend owns that state; bearer = Firebase ID token);
 *  - optimistic status feedback -> parent hook patch callbacks until the
 *    realtime mirror catches up.
 */
import { useEffect, useState } from "react";
import {
  App,
  Button,
  DatePicker,
  Descriptions,
  Drawer,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import type { Lead, LeadWorkflowStatus } from "@/domain/crm/lead";
import type { CrmNoteStorePort } from "@/domain/crm/ports/crmNoteStorePort";
import {
  CrmApiClientError,
  fetchRevealedPhoneNumber,
  searchCustomerByPhone,
  updateLeadStatus,
  withdrawMarketingConsent,
  type CrmCustomerProfile,
} from "./crmApiClient";
import type { OptimisticLeadPatch } from "./leadStreamProjection";
import {
  leadStatusActionOptions,
  leadStatusDisplayColor,
  leadStatusDisplayLabel,
} from "./leadStatusDisplay";

export interface CustomerDetailProps {
  open: boolean;
  /** Lead selected in the table; anchors the drawer. */
  anchorLead: Lead | null;
  /** Same-customer leads derived from the live stream (masked-phone match). */
  customerLeads: readonly Lead[];
  /** Fresh Firebase ID token for the authorized CRM endpoints. */
  bearerToken: string | null;
  /** Firebase uid of the signed-in staff member (note author). */
  currentStaffUid: string | null;
  /** FIRESTORE-NATIVE note store port (see crmNoteStorePort.ts). */
  noteStore: CrmNoteStorePort;
  onOptimisticLeadPatch: (leadId: string, patch: OptimisticLeadPatch) => void;
  onClearOptimisticLeadPatch: (leadId: string) => void;
  onClose: () => void;
}

interface RejectionDraft {
  reason: string;
  reengageDate: Dayjs | null;
}

export function CustomerDetail({
  open,
  anchorLead,
  customerLeads,
  bearerToken,
  currentStaffUid,
  noteStore,
  onOptimisticLeadPatch,
  onClearOptimisticLeadPatch,
  onClose,
}: CustomerDetailProps) {
  const { message } = App.useApp();

  const [customerProfile, setCustomerProfile] =
    useState<CrmCustomerProfile | null>(null);
  const [revealedPhoneNumber, setRevealedPhoneNumber] = useState<string | null>(
    null
  );
  const [rawPhoneSearchValue, setRawPhoneSearchValue] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [statusUpdating, setStatusUpdating] = useState(false);
  const [rejectionDraft, setRejectionDraft] = useState<RejectionDraft | null>(
    null
  );
  const [marketingConsentWithdrawn, setMarketingConsentWithdrawn] = useState(
    false
  );

  // Reset the per-customer state whenever the drawer reopens on another lead.
  useEffect(() => {
    if (!open || !anchorLead) {
      return;
    }
    setCustomerProfile(null);
    setRevealedPhoneNumber(null);
    setRawPhoneSearchValue("");
    setRejectionDraft(null);
    setMarketingConsentWithdrawn(
      anchorLead.marketingWithdrawnAt !== null
    );
    setNoteContent("");
    let noteLoadCancelled = false;
    noteStore
      .readLeadNote(anchorLead.id)
      .then((snapshot) => {
        if (!noteLoadCancelled && snapshot !== null) {
          setNoteContent(snapshot.content);
        }
      })
      .catch(() => {
        // A failed note read must not block the drawer; the editor starts
        // empty and saving re-creates the document.
      });
    return () => {
      noteLoadCancelled = true;
    };
  }, [open, anchorLead, noteStore]);

  if (!anchorLead) {
    return (
      <Drawer open={open} onClose={onClose} title="Chi tiết khách hàng" width={520}>
        <Typography.Text type="secondary">Chưa chọn lead.</Typography.Text>
      </Drawer>
    );
  }

  // Handlers below run after async boundaries, where TS loses the guard's
  // narrowing; this local keeps them honest.
  const selectedLead = anchorLead;

  function requireBearerToken(): string | null {
    if (bearerToken === null) {
      message.error("Chưa có phiên đăng nhập hợp lệ. Vui lòng tải lại trang.");
      return null;
    }
    return bearerToken;
  }

  async function handleSearchCustomerByPhone(rawPhone: string): Promise<void> {
    const token = requireBearerToken();
    if (token === null || rawPhone.trim() === "") {
      return;
    }
    try {
      const profile = await searchCustomerByPhone({
        rawPhone: rawPhone.trim(),
        bearerToken: token,
      });
      setCustomerProfile(profile);
      message.success("Đã tìm thấy khách hàng.");
    } catch (error) {
      message.error(
        error instanceof CrmApiClientError ? error.message : "Tra cứu thất bại."
      );
    }
  }

  async function handleRevealPhoneNumber(): Promise<void> {
    const token = requireBearerToken();
    if (token === null || customerProfile === null) {
      return;
    }
    try {
      const phone = await fetchRevealedPhoneNumber({
        customerId: customerProfile.customerId,
        bearerToken: token,
      });
      setRevealedPhoneNumber(phone);
    } catch (error) {
      message.error(
        error instanceof CrmApiClientError ? error.message : "Không xem được số điện thoại."
      );
    }
  }

  async function handleUpdateLeadStatus(request: {
    status: LeadWorkflowStatus;
    rejectionReason?: string;
    reengageAt?: string;
  }): Promise<void> {
    const token = requireBearerToken();
    if (token === null) {
      return;
    }
    setStatusUpdating(true);
    onOptimisticLeadPatch(selectedLead.id, {
      workflowStatus: request.status,
      rejectionReason: request.rejectionReason ?? null,
      reengageAt: request.reengageAt ?? null,
    });
    try {
      await updateLeadStatus({
        leadId: selectedLead.id,
        bearerToken: token,
        status: request.status,
        rejectionReason: request.rejectionReason,
        reengageAt: request.reengageAt,
      });
      message.success("Đã cập nhật trạng thái.");
    } catch (error) {
      onClearOptimisticLeadPatch(selectedLead.id);
      message.error(
        error instanceof CrmApiClientError
          ? error.message
          : "Cập nhật trạng thái thất bại."
      );
    } finally {
      setStatusUpdating(false);
    }
  }

  async function handleSaveNote(): Promise<void> {
    if (currentStaffUid === null) {
      message.error("Chưa xác định được tài khoản đang đăng nhập.");
      return;
    }
    setNoteSaving(true);
    try {
      // FIRESTORE-NATIVE write: notes/{leadId} straight from the staff client.
      await noteStore.saveLeadNote({
        leadId: selectedLead.id,
        content: noteContent,
        authorUid: currentStaffUid,
      });
      message.success("Đã lưu ghi chú.");
    } catch {
      message.error("Lưu ghi chú thất bại.");
    } finally {
      setNoteSaving(false);
    }
  }

  async function handleWithdrawMarketingConsent(): Promise<void> {
    const token = requireBearerToken();
    if (token === null || customerProfile === null) {
      return;
    }
    try {
      await withdrawMarketingConsent({
        customerId: customerProfile.customerId,
        bearerToken: token,
      });
      setMarketingConsentWithdrawn(true);
      message.success("Đã ngừng liên hệ marketing với khách hàng này.");
    } catch (error) {
      message.error(
        error instanceof CrmApiClientError ? error.message : "Ngừng liên hệ thất bại."
      );
    }
  }

  const statusOptions = leadStatusActionOptions(anchorLead.workflowStatus);
  const displayedCustomerRows =
    customerProfile !== null
      ? customerProfile.leads.map((row) => ({
          key: row.id,
          projectKey: row.project_key,
          statusLabel: row.status,
          createdAt: row.created_at,
        }))
      : customerLeads.map((lead) => ({
          key: lead.id,
          projectKey: lead.projectKey,
          statusLabel: leadStatusDisplayLabel(lead.workflowStatus),
          createdAt: lead.updatedAt,
        }));

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={560}
      title={`Chi tiết khách hàng — ${anchorLead.name ?? anchorLead.maskedPhone ?? anchorLead.id}`}
    >
      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <Descriptions
          column={1}
          size="small"
          bordered
          items={[
            {
              key: "maskedPhone",
              label: "Số điện thoại",
              children: (
                <Space>
                  <span>{revealedPhoneNumber ?? anchorLead.maskedPhone ?? "—"}</span>
                  {revealedPhoneNumber !== null ? (
                    <Tag color="purple">Đã hiển thị đầy đủ</Tag>
                  ) : null}
                </Space>
              ),
            },
            {
              key: "project",
              label: "Dự án",
              children: anchorLead.projectKey,
            },
            {
              key: "budget",
              label: "Ngân sách (VNĐ)",
              children:
                anchorLead.budgetVnd !== null
                  ? new Intl.NumberFormat("vi-VN").format(anchorLead.budgetVnd)
                  : "—",
            },
            {
              key: "consent",
              label: "Đồng ý nhận tin",
              children: (
                <Space>
                  {anchorLead.consentFlags.consentService ? (
                    <Tag color="green">Dịch vụ</Tag>
                  ) : null}
                  {anchorLead.consentFlags.consentMarketing &&
                  !marketingConsentWithdrawn ? (
                    <Tag color="blue">Marketing</Tag>
                  ) : null}
                  {marketingConsentWithdrawn ? (
                    <Tag color="red">Đã ngừng liên hệ</Tag>
                  ) : null}
                </Space>
              ),
            },
          ]}
        />

        <Space direction="vertical" style={{ width: "100%" }} size="small">
          <Typography.Text strong>Số điện thoại khách hàng</Typography.Text>
          {customerProfile === null ? (
            <Space.Compact style={{ width: "100%" }}>
              <Input
                placeholder="Nhập số điện thoại để tra cứu khách hàng"
                value={rawPhoneSearchValue}
                onChange={(event) => setRawPhoneSearchValue(event.target.value)}
                onPressEnter={() => handleSearchCustomerByPhone(rawPhoneSearchValue)}
              />
              <Button
                type="primary"
                onClick={() => handleSearchCustomerByPhone(rawPhoneSearchValue)}
              >
                Tra cứu
              </Button>
            </Space.Compact>
          ) : revealedPhoneNumber === null ? (
            <Button onClick={handleRevealPhoneNumber}>Xem số đầy đủ</Button>
          ) : null}
        </Space>

        <Space direction="vertical" style={{ width: "100%" }} size="small">
          <Typography.Text strong>Trạng thái lead</Typography.Text>
          <Select<LeadWorkflowStatus>
            style={{ width: "100%" }}
            value={anchorLead.workflowStatus}
            options={statusOptions}
            loading={statusUpdating}
            onChange={(nextStatus) => {
              // "lost" is the domain rejection state; it requires a reason,
              // collected through the modal below before anything is sent.
              if (nextStatus === "lost") {
                setRejectionDraft({ reason: "", reengageDate: null });
                return;
              }
              void handleUpdateLeadStatus({ status: nextStatus });
            }}
          />
          {anchorLead.workflowStatus === "lost" ? (
            <Typography.Text type="secondary">
              {`Lý do từ chối: ${anchorLead.rejectionReason ?? "—"}`}
              {anchorLead.reengageAt !== null
                ? ` — hẹn gọi lại ${dayjs(anchorLead.reengageAt).format("DD/MM/YYYY")}`
                : ""}
            </Typography.Text>
          ) : null}
        </Space>

        <Space direction="vertical" style={{ width: "100%" }} size="small">
          <Typography.Text strong>Ghi chú nội bộ</Typography.Text>
          <Input.TextArea
            rows={4}
            value={noteContent}
            onChange={(event) => setNoteContent(event.target.value)}
            placeholder="Ghi chú về khách hàng (chỉ nhân viên xem được)"
          />
          <Button
            type="primary"
            loading={noteSaving}
            onClick={() => void handleSaveNote()}
          >
            Lưu ghi chú
          </Button>
        </Space>

        <Space direction="vertical" style={{ width: "100%" }} size="small">
          <Typography.Text strong>Lead của khách hàng</Typography.Text>
          <List
            size="small"
            bordered
            dataSource={displayedCustomerRows}
            locale={{ emptyText: "Chưa có lead nào." }}
            renderItem={(row) => (
              <List.Item>
                <Space style={{ justifyContent: "space-between", width: "100%" }}>
                  <span>{row.projectKey}</span>
                  <Space>
                    <Tag>{row.statusLabel}</Tag>
                    <Typography.Text type="secondary">
                      {row.createdAt !== null
                        ? dayjs(row.createdAt).format("DD/MM/YYYY")
                        : "—"}
                    </Typography.Text>
                  </Space>
                </Space>
              </List.Item>
            )}
          />
        </Space>

        <Popconfirm
          title="Ngừng liên hệ khách hàng này?"
          description="Khách hàng sẽ không còn nhận tin marketing. Hành động không thể hoàn tác."
          okText="Ngừng liên hệ"
          okButtonProps={{ danger: true }}
          cancelText="Hủy"
          disabled={customerProfile === null || marketingConsentWithdrawn}
          onConfirm={() => void handleWithdrawMarketingConsent()}
        >
          <Button
            danger
            disabled={customerProfile === null || marketingConsentWithdrawn}
          >
            Ngừng liên hệ
          </Button>
        </Popconfirm>
        {customerProfile === null ? (
          <Typography.Text type="secondary">
            Tra cứu số điện thoại trước khi ngừng liên hệ.
          </Typography.Text>
        ) : null}
      </Space>

      <Modal
        open={rejectionDraft !== null}
        title="Lý do từ chối lead"
        okText="Lưu từ chối"
        cancelText="Hủy"
        onCancel={() => setRejectionDraft(null)}
        onOk={() => {
          if (rejectionDraft === null) {
            return;
          }
          if (rejectionDraft.reason.trim() === "") {
            message.warning("Vui lòng nhập lý do từ chối.");
            return;
          }
          setRejectionDraft(null);
          void handleUpdateLeadStatus({
            status: "lost",
            rejectionReason: rejectionDraft.reason.trim(),
            reengageAt:
              rejectionDraft.reengageDate !== null
                ? rejectionDraft.reengageDate.toISOString()
                : undefined,
          });
        }}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Input.TextArea
            rows={3}
            placeholder="Lý do từ chối (bắt buộc)"
            value={rejectionDraft?.reason ?? ""}
            onChange={(event) =>
              setRejectionDraft((previous) =>
                previous === null
                  ? previous
                  : { ...previous, reason: event.target.value }
              )
            }
          />
          <DatePicker
            style={{ width: "100%" }}
            placeholder="Hẹn gọi lại (không bắt buộc)"
            value={rejectionDraft?.reengageDate ?? null}
            onChange={(date) =>
              setRejectionDraft((previous) =>
                previous === null ? previous : { ...previous, reengageDate: date }
              )
            }
          />
        </Space>
      </Modal>
    </Drawer>
  );
}
