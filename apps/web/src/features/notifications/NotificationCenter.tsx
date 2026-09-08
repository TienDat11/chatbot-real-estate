"use client";

import Link from "next/link";
import { Badge, Button, Dropdown, List } from "antd";
import { BellOutlined } from "@ant-design/icons";
import type { CSSProperties } from "react";
import { useSalesNotifications } from "@/features/notifications/SalesNotificationProvider";

const panelStyle: CSSProperties = {
  width: 340,
  maxWidth: "calc(100vw - 32px)",
  padding: 12,
  background: "var(--c-surface)",
  border: "1px solid var(--c-border)",
  borderRadius: 12,
};

const headerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: 8,
};

export function NotificationCenter() {
  const state = useSalesNotifications();
  if (!state) return null;
  const { items, unreadCount, markRead, markAllRead } = state;

  const renderPanel = () => (
    <div style={panelStyle}>
      <div style={headerStyle}>
        <strong>Thông báo</strong>
        <Button type="link" size="small" onClick={() => void markAllRead()}>
          Đánh dấu đã đọc
        </Button>
      </div>
      <List
        size="small"
        locale={{ emptyText: "Chưa có thông báo" }}
        dataSource={items.slice(0, 8)}
        renderItem={(item) => (
          <List.Item
            actions={
              item.read_at
                ? []
                : [
                    <Button key="read" type="link" size="small" onClick={() => void markRead(item.id)}>
                      Đã đọc
                    </Button>,
                  ]
            }
          >
            <Link
              href={`/sales/leads?lead=${item.lead_id}`}
              onClick={() => {
                if (!item.read_at) void markRead(item.id);
              }}
            >
              <span style={{ fontWeight: item.read_at ? 400 : 700 }}>
                {item.display_name ?? "Lead mới"}
              </span>
              <br />
              <small>{item.project_key}</small>
            </Link>
          </List.Item>
        )}
      />
    </div>
  );

  return (
    <Dropdown trigger={["click"]} popupRender={renderPanel}>
      <Badge count={unreadCount} overflowCount={99}>
        <Button
          type="text"
          icon={<BellOutlined />}
          aria-label={`Thông báo${unreadCount > 0 ? `, ${unreadCount} chưa đọc` : ""}`}
        />
      </Badge>
    </Dropdown>
  );
}
