"use client";

/**
 * Publish trigger (story 8.4 / ISSUE-07).
 *
 * Fires POST /api/admin/projects/{key}/publish with the signed-in admin's
 * Firebase ID token. The endpoint belongs to ISSUE-13's PublishProjectWorkflow
 * (wave 3); until it ships the typed 404 outcome renders an explicit "chưa
 * triển khai" notice so the button is honest about what happened.
 */
import { useState } from "react";
import { Alert, Button, Space } from "antd";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import { triggerProjectPublish } from "./projectAdminApi";

interface PublishButtonProps {
  projectKey: string;
}

export function PublishButton({ projectKey }: PublishButtonProps) {
  const [isPublishing, setIsPublishing] = useState(false);
  const [outcomeNotice, setOutcomeNotice] = useState<{
    type: "success" | "warning" | "error";
    message: string;
  } | null>(null);

  async function handlePublishClick() {
    setIsPublishing(true);
    setOutcomeNotice(null);
    try {
      const bearerToken = await getFreshIdToken();
      const outcome = await triggerProjectPublish(projectKey, bearerToken);
      switch (outcome.kind) {
        case "accepted":
          setOutcomeNotice({
            type: "success",
            message: `Đã gửi yêu cầu publish cho ${projectKey}. Quá trình chạy nền — theo dõi trạng thái trong danh sách dự án.`,
          });
          break;
        case "publishEndpointNotDeployed":
          setOutcomeNotice({
            type: "warning",
            message:
              "Backend publish chưa được triển khai (thuộc wave 3 – ISSUE-13). Dự án đã được validate phía client.",
          });
          break;
        case "forbidden":
          setOutcomeNotice({ type: "error", message: "Tài khoản không có quyền admin." });
          break;
        case "unauthenticated":
          setOutcomeNotice({
            type: "error",
            message: "Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại.",
          });
          break;
        case "network":
          setOutcomeNotice({ type: "error", message: "Không kết nối được máy chủ." });
          break;
      }
    } catch {
      setOutcomeNotice({
        type: "error",
        message: "Không lấy được phiên đăng nhập. Vui lòng đăng nhập lại.",
      });
    } finally {
      setIsPublishing(false);
    }
  }

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Button type="primary" loading={isPublishing} onClick={handlePublishClick}>
        Publish dự án
      </Button>
      {outcomeNotice ? (
        <Alert type={outcomeNotice.type} showIcon message={outcomeNotice.message} />
      ) : null}
    </Space>
  );
}
