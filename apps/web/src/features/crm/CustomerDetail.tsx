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
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { CopyOutlined, EyeInvisibleOutlined, EyeOutlined } from "@ant-design/icons";
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
} from "antd";import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import type { Lead, LeadWorkflowStatus } from "@/domain/crm/lead";
import type { CrmNoteStorePort } from "@/domain/crm/ports/crmNoteStorePort";
import {
  CrmApiClientError,
  fetchRevealedPhoneNumber,
  notifyCallStarted,
  searchCustomerByPhone,
  updateLeadStatus,
  withdrawMarketingConsent,
  type CrmCustomerProfile,
} from "@/features/crm/crmApiClient";
import type { OptimisticLeadPatch } from "@/features/crm/leadStreamProjection";
import { LeadConversationPanel } from "@/features/crm/LeadConversationPanel";
import {
  leadStatusActionOptions,
  leadStatusDisplayLabel,
} from "@/features/crm/leadStatusDisplay";

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

// The masked/revealed number and its actions share ONE line at desktop: the
// number truncates (ellipsis) instead of wrapping so the action cluster never
// drops to a second row; minWidth: 0 lets the flex item actually shrink.
// Wrapping is allowed only below the sm breakpoint (Tailwind on the row);
// at desktop the row is strictly nowrap, so reveal/conceal can never change
// the row height. Reveal state adds no badge/pin of its own — the eye icon's
// state (and aria-label) is the sole reveal indicator, keeping the matrix
// columns stable.
const phoneRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "nowrap",
  gap: 8,
  width: "100%",
  minWidth: 0,
};
const phoneTextStyle: CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  minWidth: 0,
};
// Icon-only actions never shrink (their fixed footprint keeps the reveal/
// conceal toggle from reflowing the row); the phone text is the sole flexible
// item and truncates instead of pushing the actions to a second line.
const phoneActionStyle: CSSProperties = { flexShrink: 0 };

/**
 * Opaque customer identity the drawer can act on WITHOUT the manual phone
 * lookup. Two ownership proofs, in order of strength:
 *  1. the row carries the backend customer_id (HMAC of the phone) as a FIELD:
 *     the server-paged REST listing sets it only on rows already scoped to
 *     the caller's own leads, and the realtime stream sets it only within the
 *     caller's where-scoped subset, so its presence alone authorizes the
 *     reveal/consent routes (the backend re-enforces ownership regardless);
 *  2. legacy mirrors predate the customer_id FIELD: their document id WAS the
 *     customer digest, but the id alone proves nothing, so the isolation uid
 *     must match the current staff.
 * Ownership — not lifecycle status — is the gate, mirroring the backend rule
 * that only the assigning sales (or an admin via the explicit lookup path)
 * may act on a customer.
 */
function directRevealCustomerIdOf(
  lead: Lead | null,
  currentStaffUid: string | null
): string | null {
  if (lead === null || currentStaffUid === null) {
    return null;
  }
  if (lead.customerId) {
    return lead.customerId;
  }
  if (lead.assignedSalesFirebaseUid === currentStaffUid) {
    return lead.id;
  }
  return null;
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
  // Single source of the revealed plaintext; conceal clears it entirely so
  // no plaintext lingers in memory (each re-reveal re-authorizes via API).
  const [revealedPhoneNumber, setRevealedPhoneNumber] = useState<string | null>(
    null
  );
  const [phoneAction, setPhoneAction] = useState<"idle" | "revealing" | "copying">("idle");
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
  const previousAuthRef = useRef({ bearerToken, currentStaffUid });
  const revealRequestGenerationRef = useRef(0);
  // Synchronous re-entry locks (security-wave review): both actions below
  // await an async token mint before doing anything, and a React state flip
  // is invisible to same-batch double invocations — only a ref set BEFORE the
  // await can reject a rapid second activation.
  const revealInFlightRef = useRef(false);
  const statusUpdateInFlightRef = useRef(false);
  // Drawer identity (open + lead id) of the previous reset effect run; the
  // reset fires only when this changes, never on a realtime patch that merely
  // replaces the anchorLead object (a patch must not wipe a revealed phone).
  const previousDrawerIdentityRef = useRef<{
    open: boolean;
    leadId: string | null;
  }>({ open: false, leadId: null });
  // Invalidate a stale note read after the drawer switches to another lead.
  const noteLoadGenerationRef = useRef(0);
  // Effective customer identity without a manual lookup: the searched profile
  // wins, otherwise the current staff's own assigned lead carries it directly.
  const directRevealCustomerId = directRevealCustomerIdOf(
    anchorLead,
    currentStaffUid
  );
  const effectiveCustomerId =
    customerProfile !== null ? customerProfile.customerId : directRevealCustomerId;
  const revealIdentityRef = useRef({
    open,
    leadId: anchorLead?.id ?? null,
    customerId: effectiveCustomerId,
    bearerToken,
    currentStaffUid,
  });
  useLayoutEffect(() => {
    revealIdentityRef.current = {
      open,
      leadId: anchorLead?.id ?? null,
      customerId: effectiveCustomerId,
      bearerToken,
      currentStaffUid,
    };
  }, [
    open,
    anchorLead?.id,
    effectiveCustomerId,
    bearerToken,
    currentStaffUid,
  ]);

  // Raw phone data must disappear before an auth change can paint another frame.
  useLayoutEffect(() => {
    const previousAuth = previousAuthRef.current;
    if (
      bearerToken === null ||
      currentStaffUid === null ||
      previousAuth.bearerToken !== bearerToken ||
      previousAuth.currentStaffUid !== currentStaffUid
    ) {
      setRevealedPhoneNumber(null);
      setPhoneAction("idle");
    }
    previousAuthRef.current = { bearerToken, currentStaffUid };
  }, [bearerToken, currentStaffUid]);

  // Reset the per-customer state only when the drawer identity changes
  // (opens/closes or switches lead), synchronously: a deferred reset would
  // race an immediate reveal and clear the freshly returned phone.
  useEffect(() => {
    const leadId = anchorLead?.id ?? null;
    const previousIdentity = previousDrawerIdentityRef.current;
    previousDrawerIdentityRef.current = { open, leadId };
    if (open === previousIdentity.open && leadId === previousIdentity.leadId) {
      return;
    }
    setCustomerProfile(null);
    setRevealedPhoneNumber(null);
    setPhoneAction("idle");
    setRawPhoneSearchValue("");
    setRejectionDraft(null);
    setNoteContent("");
    if (!open || anchorLead === null) {
      return;
    }
    // Syncing consent state synchronously on drawer-identity change is
    // intentional: a deferred reset would race an immediate reveal (see the
    // effect comment above) — same suppression precedent as LeadConversationPanel.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMarketingConsentWithdrawn(anchorLead.marketingWithdrawnAt !== null);
    const noteLoadGeneration = ++noteLoadGenerationRef.current;
    noteStore
      .readLeadNote(anchorLead.id)
      .then((snapshot) => {
        if (
          noteLoadGeneration === noteLoadGenerationRef.current &&
          snapshot !== null
        ) {
          setNoteContent(snapshot.content);
        }
      })
      .catch(() => {
        // A failed note read must not block the drawer; the editor starts
        // empty and saving re-creates the document.
      });
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

  function requireBearerToken(): Promise<string | null> {
    // The workspace bearer prop is captured once at mount, and Firebase ID
    // tokens expire after ~1h — a drawer left open past expiry would replay a
    // stale bearer and 401. Each action mints its own token instead; a failed
    // mint (signed out) blocks the request and reuses the reveal path's
    // actionable message instead of leaking a bearer-less call.
    return getFreshIdToken().catch(() => null).then((token) => {
      if (token === null) {
        message.error("Chưa có phiên đăng nhập hợp lệ. Vui lòng tải lại trang.");
      }
      return token;
    });
  }

  async function handleSearchCustomerByPhone(rawPhone: string): Promise<void> {
    const token = await requireBearerToken();
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
    // Lock acquired synchronously BEFORE the async mint: the phoneAction state
    // flips only later, so a rapid double-click would otherwise pass this
    // guard twice and start two reveals.
    if (effectiveCustomerId === null || revealInFlightRef.current) {
      return;
    }
    revealInFlightRef.current = true;
    try {
      const requestGeneration = ++revealRequestGenerationRef.current;
      const requestIdentity = {
        open,
        leadId: selectedLead.id,
        customerId: effectiveCustomerId,
        bearerToken,
        currentStaffUid,
      };
      const token = await getFreshIdToken().catch(() => null);
      if (token === null) {
        message.error("Chưa có phiên đăng nhập hợp lệ. Vui lòng tải lại trang.");
        return;
      }
      setPhoneAction("revealing");
      try {
        const phone = await fetchRevealedPhoneNumber({
          customerId: requestIdentity.customerId,
          bearerToken: token,
        });
        const currentIdentity = revealIdentityRef.current;
        if (
          requestGeneration === revealRequestGenerationRef.current &&
          requestIdentity.open === currentIdentity.open &&
          requestIdentity.leadId === currentIdentity.leadId &&
          requestIdentity.customerId === currentIdentity.customerId &&
          requestIdentity.bearerToken === currentIdentity.bearerToken &&
          requestIdentity.currentStaffUid === currentIdentity.currentStaffUid &&
          requestIdentity.bearerToken !== null &&
          requestIdentity.currentStaffUid !== null
        ) {
          setRevealedPhoneNumber(phone);
        }
      } catch (error) {
        message.error(
          error instanceof CrmApiClientError ? error.message : "Không xem được số điện thoại."
        );
      } finally {
        setPhoneAction("idle");
      }
    } finally {
      // Released on every exit path, including the failed-mint early return.
      revealInFlightRef.current = false;
    }
  }

  async function handleCopyPhoneNumber(): Promise<void> {
    if (revealedPhoneNumber === null || phoneAction === "copying") {
      return;
    }
    setPhoneAction("copying");
    try {
      await navigator.clipboard.writeText(revealedPhoneNumber);
      message.success("Đã sao chép số điện thoại.");
    } catch {
      message.error("Không thể sao chép số điện thoại.");
    } finally {
      setPhoneAction("idle");
    }
  }

  function handleConcealPhoneNumber(): void {
    // Conceal needs no authorization: it only removes plaintext from the UI
    // and memory. Any later re-reveal goes through the authorized endpoint.
    setRevealedPhoneNumber(null);
  }

  async function handleUpdateLeadStatus(request: {
    status: LeadWorkflowStatus;
    rejectionReason?: string;
    reengageAt?: string;
  }): Promise<void> {
    // Re-entry rejected synchronously BEFORE the async mint: one PATCH at a
    // time means a first failure's optimistic rollback can never wipe a second
    // request's state, and rapid double activations cannot double-fire.
    if (statusUpdateInFlightRef.current) {
      return;
    }
    statusUpdateInFlightRef.current = true;
    try {
      const token = await requireBearerToken();
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
          numericLeadId: selectedLead.leadId ?? undefined,
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
    } finally {
      // Released on every exit path, including the failed-mint early return.
      statusUpdateInFlightRef.current = false;
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
    const token = await requireBearerToken();
    // Same identity rule as the reveal: own assigned leads act directly on
    // their opaque customer_id without the manual lookup round-trip.
    if (token === null || effectiveCustomerId === null) {
      return;
    }
    try {
      await withdrawMarketingConsent({
        customerId: effectiveCustomerId,
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
          // Locked label track: the label column keeps an identical width
          // whether the value cell holds a masked number, a revealed number,
          // or the action cluster. The value line is nowrap+ellipsis (min-width
          // 0), so its intrinsic width stays small and the label can never be
          // squeezed — toggling reveal does not reflow the matrix.
          labelStyle={{ width: 140 }}
          items={[
            {
              key: "maskedPhone",
              label: "Số điện thoại",
              children:
                revealedPhoneNumber !== null ? (
                  <div style={phoneRowStyle}>
                    <a
                      href={`tel:${revealedPhoneNumber}`}
                      aria-label="Gọi số điện thoại khách hàng"
                      style={phoneTextStyle}
                      onClick={() => {
                        if (selectedLead.leadId === undefined) return;
                        // Best-effort side effect, but it still must never
                        // leave bearer-less: the drawer prop can go stale, so
                        // mint per click like every other drawer action.
                        void getFreshIdToken().catch(() => null).then((token) => {
                          if (token === null) return undefined;
                          return notifyCallStarted({ leadId: Number(selectedLead.leadId), bearerToken: token }).then((result) => {
                            if (!result.customerHasRegisteredDevices) message.info("Khách chưa bật thông báo");
                          });
                        }).catch(() => undefined);
                      }}
                    >
                      {revealedPhoneNumber}
                    </a>
                    {/* Slashed eye = conceal: removes the plaintext without
                        any API round-trip; the icon mirrors the action. The
                        eye's state (and aria-label) is the sole reveal
                        indicator — no badge/pin, so the row footprint is
                        identical before and after reveal. */}
                    <Button
                      size="small"
                      type="text"
                      style={phoneActionStyle}
                      aria-label="Ẩn số điện thoại"
                      title="Ẩn số điện thoại khách hàng"
                      icon={<EyeInvisibleOutlined />}
                      onClick={handleConcealPhoneNumber}
                    />
                    <Button
                      size="small"
                      type="text"
                      style={phoneActionStyle}
                      aria-label="Sao chép số điện thoại"
                      title="Sao chép số điện thoại khách hàng"
                      icon={<CopyOutlined />}
                      onClick={() => void handleCopyPhoneNumber()}
                      loading={phoneAction === "copying"}
                    />
                  </div>
                ) : (
                  <div style={phoneRowStyle}>
                    <span style={phoneTextStyle}>
                      {anchorLead.maskedPhone ?? "-"}
                    </span>
                    {/* Eye = reveal: the full number appears only after the
                        authorized endpoint returns it; loading swaps the icon
                        for the spinner and keeps the button single-click. */}
                    <Button
                      size="small"
                      type="text"
                      style={phoneActionStyle}
                      aria-label="Hiện số đầy đủ"
                      title="Hiện số điện thoại đầy đủ"
                      icon={<EyeOutlined />}
                      loading={phoneAction === "revealing"}
                      onClick={() => void handleRevealPhoneNumber()}
                      disabled={effectiveCustomerId === null}
                    />
                  </div>
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

        {/* Manual lookup only when the drawer has no direct customer identity
            (lead not assigned to the current staff): for own assigned leads the
            opaque customer_id is already known, so searching by phone would be
            a redundant PII round-trip. */}
        {directRevealCustomerId === null ? (
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
            ) : null}
          </Space>
        ) : null}

        {/* Sales context: what the customer asked before leaving their phone.
            Read-only transcript; the phone above stays the primary contact info. */}
        <LeadConversationPanel
          leadId={selectedLead.id}
          numericLeadId={selectedLead.leadId ?? undefined}
          bearerToken={bearerToken}
        />

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
          disabled={effectiveCustomerId === null || marketingConsentWithdrawn}
          onConfirm={() => void handleWithdrawMarketingConsent()}
        >
          <Button
            danger
            disabled={effectiveCustomerId === null || marketingConsentWithdrawn}
          >
            Ngừng liên hệ
          </Button>
        </Popconfirm>
        {effectiveCustomerId === null ? (
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
