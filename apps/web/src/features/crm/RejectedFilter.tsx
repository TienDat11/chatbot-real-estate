"use client";

/**
 * RejectedFilter — CRM toolbar for the sales-leads view (story 9.3).
 *
 * Pure presentation over LeadToolbarFilterCriteria (logic lives in
 * crmLeadFilters.ts). The project select doubles as the workspace's project
 * scope control: null means "every active project", which fans the realtime
 * stream out to one subscription per project. The status selector covers the
 * full workflow domain (Tất cả + every LeadWorkflowStatus with its display
 * label), and the reengage date window is always enabled — it composes with
 * the status filter independently.
 */
import { DatePicker, Select, Typography } from "antd";
import dayjs from "dayjs";
import type { LeadWorkflowStatus } from "@/domain/crm/lead";
import type { LeadToolbarFilterCriteria } from "./crmLeadFilters";
import { leadStatusSelectOptions } from "./leadStatusDisplay";

/** Sentinel option value meaning "no status filter" (Tất cả). */
const ALL_STATUSES_VALUE = "all";

export interface RejectedFilterProps {
  projectOptions: { value: string; label: string }[];
  /** Currently scoped project key; null = every active project. */
  selectedProjectKey: string | null;
  onSelectProjectKey: (projectKey: string | null) => void;
  /** Selected workflow status; null = Tất cả (no status filter). */
  statusFilter: LeadWorkflowStatus | null;
  onStatusFilterChange: (status: LeadWorkflowStatus | null) => void;
  criteria: LeadToolbarFilterCriteria;
  onCriteriaChange: (criteria: LeadToolbarFilterCriteria) => void;
  /** Row count left after the full filter chain (live feedback). */
  matchedLeadCount: number;
}

export function RejectedFilter({
  projectOptions,
  selectedProjectKey,
  onSelectProjectKey,
  statusFilter,
  onStatusFilterChange,
  criteria,
  onCriteriaChange,
  matchedLeadCount,
}: RejectedFilterProps) {
  return (
    <div
      className="crm-rejected-filter-grid"
      data-testid="rejected-filter-grid"
    >
      <Select
        allowClear
        placeholder="Tất cả dự án"
        style={{ width: "100%", minWidth: 200 }}
        value={selectedProjectKey}
        options={projectOptions}
        onChange={(value) => onSelectProjectKey(value ?? null)}
      />
      <Select
        aria-label="Trạng thái lead"
        data-testid="lead-status-select"
        style={{ width: "100%", minWidth: 160 }}
        value={statusFilter ?? ALL_STATUSES_VALUE}
        options={[
          { value: ALL_STATUSES_VALUE, label: "Tất cả trạng thái" },
          ...leadStatusSelectOptions(),
        ]}
        onChange={(value: LeadWorkflowStatus | typeof ALL_STATUSES_VALUE) =>
          onStatusFilterChange(
            value === ALL_STATUSES_VALUE ? null : (value as LeadWorkflowStatus)
          )
        }
      />
      <div data-testid="lead-filter-trailing-group">
        <DatePicker.RangePicker
          allowEmpty={[true, true]}
          data-testid="lead-reengage-range"
          placeholder={["Hẹn gọi lại từ", "đến"]}
          style={{ width: "100%" }}
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
        <Typography.Text type="secondary">
          {`Số lead khớp bộ lọc: ${matchedLeadCount}`}
        </Typography.Text>
      </div>
    </div>
  );
}
