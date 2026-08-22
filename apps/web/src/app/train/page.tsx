import { App as AntdApp } from "antd";
import { AuthProvider } from "@/lib/AuthProvider";
import { RequireRole } from "@/components/RequireRole";
import { TrainWorkspace } from "@/features/train/TrainWorkspace";

/**
 * Training chat page (story 11.2). Server component shell: AuthProvider +
 * RequireRole gate the route to sales only, and AntdApp enables the
 * message/notification contexts for the client workspace. The workspace
 * itself carries the training-mode chat (no selling surfaces).
 */
export default function TrainPage() {
  return (
    <AuthProvider>
      <RequireRole allowedRoles={["sales"]}>
        <AntdApp>
          <TrainWorkspace />
        </AntdApp>
      </RequireRole>
    </AuthProvider>
  );
}
