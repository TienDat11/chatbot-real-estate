"use client";

/**
 * Admin CMS workspace (story 8.4 / ISSUE-07): tabs for the active-project
 * catalogue and the new-project draft form. Client-side because both tabs
 * need browser state (fetch + file uploads).
 */
import { Card, Col, Row, Tabs, Typography } from "antd";
import { SafetyCertificateOutlined } from "@ant-design/icons";
import { C, RADIUS, SHADOW } from "@/lib/tokens";
import { ProjectForm } from "./ProjectForm";
import { ProjectList } from "./ProjectList";

export function AdminWorkspace() {
  return (
    <main style={{ padding: 0, minHeight: "100vh", background: C.bg }}>
      <div
        className="admin-shell"
        style={{
          padding: "28px 32px 22px",
          display: "flex",
          alignItems: "center",
          gap: 14,
        }}
      >
        <div
          className="hero-badge"
          style={{
            width: 44,
            height: 44,
            borderRadius: RADIUS.small,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 22,
            flexShrink: 0,
          }}
        >
          <SafetyCertificateOutlined />
        </div>
        <div style={{ minWidth: 0 }}>
          <Typography.Text
            className="eyebrow"
            style={{ display: "block", color: C.gold, marginBottom: 2 }}
          >
            Quản trị CMS
          </Typography.Text>
          <Typography.Title level={3} style={{ margin: 0, color: C.onDark, fontWeight: 700 }}>
            Quản trị dự án
          </Typography.Title>
        </div>
      </div>
      <div style={{ padding: "24px 32px 40px" }}>
        <Row gutter={[16, 16]}>
          <Col span={24}>
            <Card
              size="small"
              style={{
                borderRadius: RADIUS.card,
                boxShadow: SHADOW.card,
                borderColor: C.border,
                overflow: "hidden",
              }}
            >
              <Tabs
                defaultActiveKey="projects"
                items={[
                  {
                    key: "projects",
                    label: "Dự án đang hoạt động",
                    children: <ProjectList />,
                  },
                  {
                    key: "new-project",
                    label: "Thêm dự án mới",
                    children: <ProjectForm />,
                  },
                ]}
              />
            </Card>
          </Col>
        </Row>
      </div>
    </main>
  );
}
