import { App as AntdApp } from "antd";
import { AuthProvider } from "@/lib/AuthProvider";
import { RequireRole } from "@/components/RequireRole";
import { RealtimeProvider } from "@/lib/realtime/RealtimeProvider";
import { CrmWorkspace } from "@/features/crm/CrmWorkspace";

/**
 * CRM realtime page (story 9.3). Server component shell: AuthProvider +
 * RequireRole guard the route (admin/sales), RealtimeProvider mounts the
 * port-typed composition root, and AntdApp enables message/notification
 * contexts for the client workspace. Only the workspace itself is client-side.
 */
export default function CrmPage() {
  return (
    <AuthProvider>
      <RequireRole allowedRoles={["admin", "sales"]}>
        <RealtimeProvider>
          <AntdApp>
            <CrmWorkspace />
          </AntdApp>
        </RealtimeProvider>
      </RequireRole>
    </AuthProvider>
  );
}
