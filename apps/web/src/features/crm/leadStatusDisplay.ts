/**
 * Vietnamese display metadata for lead workflow statuses (UI strings only —
 * the status VALUES stay the English domain literals shared with the backend).
 */
import { LEAD_WORKFLOW_STATUSES, type LeadWorkflowStatus } from "@/domain/crm/lead";

export interface LeadStatusDisplayEntry {
  label: string;
  /** antd Tag color token. */
  color: string;
}

const LEAD_STATUS_DISPLAY_BY_STATUS: Record<LeadWorkflowStatus, LeadStatusDisplayEntry> = {
  new: { label: "Khách mới", color: "blue" },
  assigned: { label: "Đã gán", color: "cyan" },
  called: { label: "Đã gọi", color: "geekblue" },
  callback: { label: "Gọi lại sau", color: "gold" },
  no_answer: { label: "Không nghe máy", color: "orange" },
  booked: { label: "Đã đặt lịch", color: "green" },
  lost: { label: "Từ chối", color: "red" },
  expired: { label: "Hết hạn", color: "default" },
};

/** Statuses the CRM status PATCH accepts (same set as the backend pattern). */
const CRM_ACTIONABLE_STATUSES: readonly LeadWorkflowStatus[] = [
  "called",
  "callback",
  "booked",
  "lost",
];

/** Vietnamese label for a workflow status (falls back to the raw value). */
export function leadStatusDisplayLabel(status: LeadWorkflowStatus): string {
  return LEAD_STATUS_DISPLAY_BY_STATUS[status]?.label ?? status;
}

/** antd Tag color for a workflow status. */
export function leadStatusDisplayColor(status: LeadWorkflowStatus): string {
  return LEAD_STATUS_DISPLAY_BY_STATUS[status]?.color ?? "default";
}

/**
 * Select options covering every workflow status, in domain order (display/
 * filtering only — the status PATCH accepts a narrower set, see below).
 */
export function leadStatusSelectOptions(): {
  value: LeadWorkflowStatus;
  label: string;
}[] {
  return LEAD_WORKFLOW_STATUSES.map((status) => ({
    value: status,
    label: leadStatusDisplayLabel(status),
  }));
}

/**
 * Options the CRM drawer's status select may SUBMIT: the backend PATCH pattern
 * is ^(called|callback|booked|lost)$. The lead's current status is always
 * included (disabled when not submittable) so the control never displays an
 * out-of-list raw value.
 */
export function leadStatusActionOptions(
  currentStatus: LeadWorkflowStatus
): { value: LeadWorkflowStatus; label: string; disabled?: boolean }[] {
  const options: {
    value: LeadWorkflowStatus;
    label: string;
    disabled?: boolean;
  }[] = CRM_ACTIONABLE_STATUSES.map((status) => ({
    value: status,
    label: leadStatusDisplayLabel(status),
  }));
  if (!CRM_ACTIONABLE_STATUSES.includes(currentStatus)) {
    options.unshift({
      value: currentStatus,
      label: leadStatusDisplayLabel(currentStatus),
      disabled: true,
    });
  }
  return options;
}
