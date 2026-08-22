"use client";

/**
 * Admin CMS workspace (story 8.4 / ISSUE-07): tabs for the active-project
 * catalogue and the new-project draft form. Client-side because both tabs
 * need browser state (fetch + file uploads).
 */
import { Card, Col, Row, Tabs, Typography } from "antd";
import { ProjectForm } from "./ProjectForm";
import { ProjectList } from "./ProjectList";

export function AdminWorkspace() {
  return (
    <main style={{ padding: 24 }}>
      <Typography.Title level={3}>Quản trị dự án</Typography.Title>
      <Row gutter={[16, 16]}>
        <Col span={24}>
          <Card size="small">
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
    </main>
  );
}
