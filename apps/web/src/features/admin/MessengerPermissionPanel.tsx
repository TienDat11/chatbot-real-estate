"use client";

/**
 * Messenger permission probe panel (Meta App Review evidence).
 *
 * Runs the read-only or full probe against the bot backend and renders one
 * row per Meta permission. The table is the evidence captured for the App
 * Review screencast, so skipped probes are shown as neutral "bỏ qua" rows
 * instead of failures.
 */
import { useState } from "react";
import { Alert, Button, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useAuth } from "@/lib/AuthProvider";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";
import {
  runPermissionProbe,
  type PermissionProbeEntry,

  type PermissionProbeOutcome,
  type PermissionProbeReport,
} from "./messengerPermissionProbe";

type PanelState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; report: PermissionProbeReport }
  | { kind: "error"; message: string };

const PROBE_RESULT_LABEL: Record<"ok" | "skipped" | "failed", string> = {
  ok: "OK",
  skipped: "Bỏ qua",
  failed: "Lỗi",
};

const PROBE_RESULT_COLOR: Record<"ok" | "skipped" | "failed", string> = {
  ok: "success",
  skipped: "default",
  failed: "error",
};

export function MessengerPermissionPanel() {
  const { user } = useAuth();
  const [state, setState] = useState<PanelState>({ kind: "idle" });
  const isRunning = state.kind === "running";

  async function handleRun() {
    setState({ kind: "running" });
    let bearerToken: string;
    try {
      bearerToken = await getFreshIdToken();
    } catch {
      setState({
        kind: "error",
        message: "Không lấy được phiên đăng nhập. Vui lòng đăng nhập lại.",
      });
      return;
    }
    const outcome: PermissionProbeOutcome = await runPermissionProbe({
      bearerToken,
    });
    setState(outcomeToState(outcome));
  }

  if (!user) {
    return (
      <Alert
        type="warning"
        showIcon
        message="Cần đăng nhập tài khoản admin để chạy kiểm tra quyền Messenger."
      />
    );
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space wrap>
        <Button type="primary" loading={isRunning} disabled={isRunning} onClick={handleRun}>
          Chạy kiểm tra quyền
        </Button>
      </Space>

      {state.kind === "error" ? <Alert type="error" showIcon message={state.message} /> : null}

      {state.kind === "done" ? (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text>
            Page ID: <Typography.Text code>{state.report.page_id}</Typography.Text>
            {"  "}API version: <Typography.Text code>{state.report.api_version}</Typography.Text>
          </Typography.Text>
          {state.report.sends_enabled ? null : (
            <Alert
              type="warning"
              showIcon
              message="Chưa cấu hình MESSENGER_PROBE_PSID nên hai quyền gửi tin sẽ bị bỏ qua."
            />
          )}
          <Table<PermissionProbeEntry>
            rowKey="permission"
            size="small"
            pagination={false}
            dataSource={state.report.probes}
            columns={PROBE_COLUMNS}
          />
        </Space>
      ) : null}
    </Space>
  );
}

const PROBE_COLUMNS: ColumnsType<PermissionProbeEntry> = [
  { title: "Permission", dataIndex: "permission", key: "permission" },
  {
    title: "Required",
    dataIndex: "required",
    key: "required",
    render: (required: boolean) =>
      required ? <Tag color="blue">Bắt buộc</Tag> : <Tag>Không cần</Tag>,
  },
  {
    title: "Result",
    key: "result",
    render: (_: unknown, probe: PermissionProbeEntry) => {
      const result = resultOf(probe);
      return <Tag color={PROBE_RESULT_COLOR[result]}>{PROBE_RESULT_LABEL[result]}</Tag>;
    },
  },
  {
    title: "HTTP status",
    dataIndex: "http_status",
    key: "http_status",
    render: (status: number | undefined) => status ?? "—",
  },
  { title: "Summary", dataIndex: "summary", key: "summary" },
];

/** Skipped is its own state: the probe never ran, so it is not a failure. */
function resultOf(probe: PermissionProbeEntry): "ok" | "skipped" | "failed" {
  if (probe.status === "skipped") return "skipped";
  return probe.ok ? "ok" : "failed";
}

function outcomeToState(outcome: PermissionProbeOutcome): PanelState {
  switch (outcome.kind) {
    case "ok":
      return { kind: "done", report: outcome.report };
    case "unauthenticated":
      return { kind: "error", message: "Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại." };
    case "forbidden":
      return { kind: "error", message: "Tài khoản không có quyền admin." };
    case "network":
      return { kind: "error", message: "Không kết nối được máy chủ." };
    case "unexpected":
      return {
        kind: "error",
        message: `Phản hồi không hợp lệ từ máy chủ${outcome.httpStatus ? ` (HTTP ${outcome.httpStatus})` : ""}.`,
      };
  }
}
