"use client";

/**
 * RejectedFilter — CRM toolbar for the rejected-leads view (story 9.3).
 *
 * Pure presentation over RejectedLeadsFilterCriteria (logic lives in
 * crmLeadFilters.ts). The project select doubles as the workspace's project
 * scope control: null means "every active project", which fans the realtime
 * stream out to one subscription per project.
 */
import { Checkbox, DatePicker, Input, Select, Space, Typography } from "antd";
import dayjs from "dayjs";
import type { RejectedLeadsFilterCriteria } from "./crmLeadFilters";

/** Criteria minus the project key — the project scope is owned by the workspace. */
export type RejectedLeadsCompanionCriteria = Omit<
  RejectedLeadsFilterCriteria,
  "projectKey"
>;

export interface RejectedFilterProps {
  projectOptions: { value: string; label: string }[];
  /** Currently scoped project key; null = every active project. */
  selectedProjectKey: string | null;
  onSelectProjectKey: (projectKey: string | null) => void;
  criteria: RejectedLeadsCompanionCriteria;
  onCriteriaChange: (criteria: RejectedLeadsCompanionCriteria) => void;
  rejectedOnly: boolean;
  onRejectedOnlyChange: (rejectedOnly: boolean) => void;
  /** Row count left after the full filter chain (live feedback). */
  matchedLeadCount: number;
}

export function RejectedFilter({
  projectOptions,
  selectedProjectKey,
  onSelectProjectKey,
  criteria,
  onCriteriaChange,
  rejectedOnly,
  onRejectedOnlyChange,
  matchedLeadCount,
}: RejectedFilterProps) {
  return (
    <Space wrap size="middle" style={{ width: "100%" }}>
      <Select
        allowClear
        placeholder="Tất cả dự án"
        style={{ minWidth: 200 }}
        value={selectedProjectKey}
        options={projectOptions}
        onChange={(value) => onSelectProjectKey(value ?? null)}
      />
      <Select
        allowClear
        placeholder="Trạng thái (tất cả)"
        style={{ minWidth: 160 }}
        value={rejectedOnly ? "rejected_only" : null}
        options={[{ value: "rejected_only", label: "Từ chối" }]}
        onChange={(value) => onRejectedOnlyChange(value === "rejected_only")}
      />
      <Input
        allowClear
        placeholder="Lý do từ chối"
        style={{ minWidth: 200 }}
        disabled={!rejectedOnly}
        value={criteria.rejectionReason ?? ""}
        onChange={(event) =>
          onCriteriaChange({
            ...criteria,
            rejectionReason: event.target.value === "" ? null : event.target.value,
          })
        }
      />
      <DatePicker.RangePicker
        allowEmpty={[true, true]}
        disabled={!rejectedOnly}
        placeholder={["Hẹn gọi lại từ", "đến"]}
        value={[
          criteria.reengageWindowFromIsoDate !== null
            ? dayjs(criteria.reengageWindowFromIsoDate)
            : null,
          criteria.reengageWindowToIsoDate !== null
            ? dayjs(criteria.reengageWindowToIsoDate)
            : null,
        ]}
        onChange={(dates) =>
          onCriteriaChange({
            ...criteria,
            reengageWindowFromIsoDate:
              dates?.[0] ? dates[0].format("YYYY-MM-DD") : null,
            reengageWindowToIsoDate:
              dates?.[1] ? dates[1].format("YYYY-MM-DD") : null,
          })
        }
      />
      <Checkbox
        checked={rejectedOnly}
        onChange={(event) => onRejectedOnlyChange(event.target.checked)}
      >
        Chỉ xem lead bị từ chối
      </Checkbox>
      <Typography.Text type="secondary">
        {`Số lead khớp bộ lọc: ${matchedLeadCount}`}
      </Typography.Text>
    </Space>
  );
}
