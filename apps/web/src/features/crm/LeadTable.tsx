"use client";

/**
 * LeadTable — server-paged lead list for the CRM page (story 9.3,
 * FE-CRM-PAGING). Rows arrive fully mapped from GET /api/crm/leads through
 * useCrmLeadsPage; pagination is SERVER-driven (keyset cursors): the pager
 * only ever offers pages that actually exist, and never slices the current
 * rows locally. The toolbar exposes the browser-notification opt-in for the
 * realtime stream (permission may only be requested from this button's click
 * — a user gesture).
 */
import { useState } from "react";
import { Badge, Button, Grid, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import type { Lead } from "@/domain/crm/lead";
import { CRM_LEADS_PAGE_SIZES } from "./useCrmLeadsPage";
import {
  browserLeadNotificationPermission,
  browserLeadNotificationPreference,
  browserNotificationsSupported,
  persistBrowserLeadNotificationPreference,
  requestBrowserLeadNotificationPermission,
  type BrowserNotificationPermission,
  type BrowserNotificationPreference,
} from "./browserLeadNotifier";
import {
  leadStatusDisplayColor,
  leadStatusDisplayLabel,
} from "./leadStatusDisplay";

export interface LeadTableProps {
  /** Rows of the CURRENT server page (mapped domain Leads). */
  rows: readonly Lead[];
  connectionState: "connecting" | "active" | "error";
  isLoading: boolean;
  /** Zero-based page index currently displayed. */
  pageIndex: number;
  pageSize: number;
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  /** Requests another EXISTING page from the server (0-based). */
  onPageChange: (pageIndex: number) => void;
  /** Requests a different page size; the server re-queries from page 0. */
  onPageSizeChange: (pageSize: number) => void;
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
  rows,
  connectionState,
  isLoading,
  pageIndex,
  pageSize,
  hasNextPage,
  hasPreviousPage,
  onPageChange,
  onPageSizeChange,
  onOpenCustomerDetail,
}: LeadTableProps) {
  const [notificationPermission, setNotificationPermission] =
    useState<BrowserNotificationPermission>(() =>
      browserLeadNotificationPermission()
    );
  const [notificationPreference, setNotificationPreference] =
    useState<BrowserNotificationPreference>(() =>
      browserLeadNotificationPreference()
    );

  // Responsive column plan: an UNPROVEN viewport (SSR / first paint) keeps the
  // full desktop set — collapsing only when matchMedia proves the viewport is
  // small avoids a desktop layout shift. Rejection is the widest free-width
  // column, so it additionally requires a large screen.
  const screens = Grid.useBreakpoint();
  const isCompactViewport = screens.md === false;
  const showRejectionColumn = screens.lg === true;

  const showNotificationOptIn =
    browserNotificationsSupported() &&
    notificationPermission === "default" &&
    notificationPreference === "unset";
  const notificationDenied =
    browserNotificationsSupported() &&
    (notificationPermission === "denied" || notificationPreference === "denied");

  const allLeadColumns: ColumnsType<Lead> = [
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
      // Keep the detail action reachable while narrow viewports scroll
      // horizontally through the remaining columns.
      fixed: "right",
      render: (_, lead) => (
        <Button type="link" onClick={() => onOpenCustomerDetail(lead.id)}>
          Chi tiết
        </Button>
      ),
    },
  ];

  // Intentional responsive visibility (acceptance: the table must never
  // overflow the viewport on tablet/mobile) — the date/number columns are the
  // widest and are dropped first; identity + status + action always stay.
  const leadColumns = allLeadColumns.filter((column) => {
    switch (column.key) {
      case "budgetVnd":
      case "reengageAt":
      case "updatedAt":
        return !isCompactViewport;
      case "rejectionReason":
        return showRejectionColumn;
      default:
        return true;
    }
  });

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
              const preference = permission === "granted" ? "enabled" : "denied";
              persistBrowserLeadNotificationPreference(preference);
              setNotificationPermission(permission);
              setNotificationPreference(preference);
            }}
          >
            Bật thông báo
          </Button>
        ) : notificationDenied ? (
          <Typography.Text type="secondary" data-testid="notification-denied-help">
            Thông báo đang bị chặn. Cho phép thông báo trong cài đặt trình duyệt để bật lại.
          </Typography.Text>
        ) : null}
      </Space>
      <Table<Lead>
        rowKey={(lead) => lead.id}
        columns={leadColumns}
        dataSource={[...rows]}
        loading={isLoading}
        // Narrow viewports scroll horizontally as a fallback instead of
        // squeezing columns until phones/actions/controls become unusable.
        scroll={{ x: "max-content" }}
        pagination={{
          // Server keyset paging: the FE never knows the total count, so the
          // pager only extends one page beyond the current one while the
          // server reports has_more. Pages are never sliced locally.
          current: pageIndex + 1,
          pageSize,
          // Stable server-paging total: never multiply by pageIndex. The FE only
          // knows the current page's rows plus the server's has_more flag, so
          // the pager offers at most one page beyond the current one (the only
          // forward page whose cursor is actually primed) and never a phantom
          // page number that maps to an undefined cursor.
          total: hasNextPage ? rows.length + pageSize : rows.length,
          showSizeChanger: true,
          pageSizeOptions: [...CRM_LEADS_PAGE_SIZES],
          onChange: (page, nextPageSize) => {
            if (nextPageSize !== pageSize) {
              onPageSizeChange(nextPageSize);
              return;
            }
            onPageChange(page - 1);
          },
        }}
        locale={{ emptyText: isLoading ? "Đang tải lead..." : "Chưa có lead nào phù hợp bộ lọc." }}
        rowClassName={() => "crm-lead-row"}
        onRow={(lead) => ({
          onClick: () => onOpenCustomerDetail(lead.id),
          style: { cursor: "pointer" },
        })}
      />
    </Space>
  );
}
