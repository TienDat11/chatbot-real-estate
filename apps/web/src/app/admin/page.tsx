import { App as AntdApp } from "antd";
import { AuthProvider } from "@/lib/AuthProvider";
import { RequireRole } from "@/components/RequireRole";
import { AdminWorkspace } from "@/features/admin/AdminWorkspace";
import { AppShell } from "@/components/AppShell";

/**
 * Admin CMS page (story 8.4 / ISSUE-07). Server component shell mirroring the
 * CRM page: AuthProvider + RequireRole gate the route to role "admin" (story
 * 8.3 claims), and only the workspace itself renders client-side.
 */
export default function AdminPage() {
  return (
    <AuthProvider>
      <RequireRole allowedRoles={["admin"]}>
        <AppShell title="Quản trị dự án" subtitle="Quản lý danh mục dự án và nội dung tra cứu">
          <AntdApp>
            <AdminWorkspace />
          </AntdApp>
        </AppShell>
      </RequireRole>
    </AuthProvider>
  );
}
