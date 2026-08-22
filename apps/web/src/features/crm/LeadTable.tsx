"use client";

/**
 * LeadTable — realtime lead list for the CRM page (story 9.3).
 *
 * Presentational: rows arrive fully projected from useCrmLeadStream (fed by
 * the Firestore realtime stack through the LeadRepositoryPort). The toolbar
 * exposes the browser-notification opt-in (permission may only be requested
 * from this button's click — a user gesture).
 */
import { useState } from "react";
import { Badge, Button, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import type { Lead } from "@/domain/crm/lead";
import {
  browserLeadNotificationPermission,
  browserNotificationsSupported,
  requestBrowserLeadNotificationPermission,
  type BrowserNotificationPermission,
} from "./browserLeadNotifier";
import {
  leadStatusDisplayColor,
  leadStatusDisplayLabel,
} from "./leadStatusDisplay";

export interface LeadTableProps {
  leads: readonly Lead[];
  connectionState: "connecting" | "active" | "error";
  /** Opens the CustomerDetail drawer anchored on this lead. */
  onOpenCustomerDetail: (leadId: string) => void;
}

const CONNECTION_BADGE: Record<LeadTableProps["connectionState"], {
  status: "processing" | "success" | "error";
  text: string;
}> = {
  connecting: { status: "processing", text: "Đang kết nối" },
  active: { status: "success", text: "Trực tiếp" },
  error: { status: "error", text: "Lỗi kết nối" },
};

function formatVietnameseDate(isoInstant: string | null): string {
  return isoInstant !== null ? dayjs(isoInstant).format("DD/MM/YYYY HH:mm") : "—";
}

function formatVndBudget(budgetVnd: number | null): string {
  return budgetVnd !== null
    ? new Intl.NumberFormat("vi-VN").format(budgetVnd)
    : "—";
}

export function LeadTable({
  leads,
  connectionState,
  onOpenCustomerDetail,
}: LeadTableProps) {
  const [notificationPermission, setNotificationPermission] =
    useState<BrowserNotificationPermission>(() =>
      browserLeadNotificationPermission()
    );

  const showNotificationOptIn =
    browserNotificationsSupported() && notificationPermission === "default";

  const leadColumns: ColumnsType<Lead> = [
    {
      title: "Khách hàng",
      key: "customer",
      render: (_, lead) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{lead.name ?? "Chưa có tên"}</Typography.Text>
          <Typography.Text type="secondary">{lead.maskedPhone ?? "—"}</Typography.Text>
        </Space>
      ),
    },
    {
      title: "Dự án",
      dataIndex: "projectKey",
      key: "projectKey",
      width: 110,
    },
    {
      title: "Trạng thái",
      key: "workflowStatus",
      width: 140,
      render: (_, lead) => (
        <Tag color={leadStatusDisplayColor(lead.workflowStatus)}>
          {leadStatusDisplayLabel(lead.workflowStatus)}
        </Tag>
      ),
    },
    {
      title: "Ngân sách (VNĐ)",
      key: "budgetVnd",
      width: 140,
      align: "right",
      render: (_, lead) => formatVndBudget(lead.budgetVnd),
    },
    {
      title: "Lý do từ chối",
      key: "rejectionReason",
      ellipsis: true,
      render: (_, lead) => lead.rejectionReason ?? "—",
    },
    {
      title: "Hẹn gọi lại",
      key: "reengageAt",
      width: 150,
      render: (_, lead) => formatVietnameseDate(lead.reengageAt),
    },
    {
      title: "Cập nhật",
      key: "updatedAt",
      width: 150,
      render: (_, lead) => formatVietnameseDate(lead.updatedAt),
    },
    {
      title: "",
      key: "actions",
      width: 110,
      render: (_, lead) => (
        <Button type="link" onClick={() => onOpenCustomerDetail(lead.id)}>
          Chi tiết
        </Button>
      ),
    },
  ];

  const badge = CONNECTION_BADGE[connectionState];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Space wrap style={{ justifyContent: "space-between", width: "100%" }}>
        <Space size="large">
          <Typography.Title level={4} style={{ margin: 0 }}>
            Danh sách lead
          </Typography.Title>
          <Badge status={badge.status} text={badge.text} />
        </Space>
        {showNotificationOptIn ? (
          <Button
            onClick={async () => {
              const permission = await requestBrowserLeadNotificationPermission();
              setNotificationPermission(permission);
            }}
          >
            Bật thông báo
          </Button>
        ) : null}
      </Space>
      <Table<Lead>
        rowKey={(lead) => lead.id}
        columns={leadColumns}
        dataSource={[...leads]}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: "Chưa có lead nào phù hợp bộ lọc." }}
        rowClassName={() => "crm-lead-row"}
        onRow={(lead) => ({
          onClick: () => onOpenCustomerDetail(lead.id),
          style: { cursor: "pointer" },
        })}
      />
    </Space>
  );
}
