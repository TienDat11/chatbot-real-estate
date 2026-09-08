"use client";

/**
 * Admin CMS workspace (story 8.4 / ISSUE-07): tabs for the active-project
 * catalogue and the new-project draft form. Client-side because both tabs
 * need browser state (fetch + file uploads).
 */
import { Card, Col, Row, Tabs } from "antd";
import { C, RADIUS, SHADOW } from "@/lib/tokens";
import { ProjectForm } from "./ProjectForm";
import { ProjectList } from "./ProjectList";

export function AdminWorkspace() {
  return (
    <div>
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
  );
}
