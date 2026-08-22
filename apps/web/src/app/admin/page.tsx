import { Card, Typography } from "antd";
import { AuthProvider } from "@/lib/AuthProvider";
import { RequireRole } from "@/components/RequireRole";

/**
 * Admin CMS placeholder (story 8.3): exists to make role gating demonstrable
 * end-to-end. The full CMS arrives in a later issue; the page itself stays a
 * server component and only the guard runs on the client.
 */
export default function AdminPage() {
  return (
    <AuthProvider>
      <RequireRole allowedRoles={["admin"]}>
        <main style={{ padding: 24 }}>
          <Card>
            <Typography.Title level={3}>Trang quản trị</Typography.Title>
            <Typography.Paragraph type="secondary">
              CMS đang xây dựng — chức năng quản lý sẽ được bổ sung trong giai đoạn tiếp theo.
            </Typography.Paragraph>
          </Card>
        </main>
      </RequireRole>
    </AuthProvider>
  );
}
