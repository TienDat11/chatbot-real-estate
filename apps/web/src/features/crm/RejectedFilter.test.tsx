// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RejectedFilter } from "./RejectedFilter";
import { EMPTY_LEAD_TOOLBAR_FILTER } from "./crmLeadFilters";

afterEach(() => cleanup());

function renderFilter() {
  return render(
    <RejectedFilter
      projectOptions={[]}
      selectedProjectKey={null}
      onSelectProjectKey={() => undefined}
      statusFilter={null}
      onStatusFilterChange={() => undefined}
      criteria={EMPTY_LEAD_TOOLBAR_FILTER}
      onCriteriaChange={() => undefined}
      matchedLeadCount={2}
    />
  );
}

describe("RejectedFilter toolbar", () => {
  it("renders all workflow statuses and an all-status option", async () => {
    renderFilter();
    const statusSelect = screen.getByTestId("lead-status-select");
    expect(statusSelect).toBeTruthy();
    fireEvent.mouseDown(statusSelect.querySelector(".ant-select-selector") ?? statusSelect);
    expect((await screen.findAllByText("Tất cả trạng thái")).length).toBeGreaterThanOrEqual(2);
    for (const label of [
      "Khách mới",
      "Đã gán",
      "Đã gọi",
      "Gọi lại sau",
      "Không nghe máy",
      "Đã đặt lịch",
      "Từ chối",
      "Hết hạn",
    ]) {
      expect(screen.getByText(label, { selector: ".ant-select-item-option-content" })).toBeTruthy();
    }
  });

  it("does not render the removed rejected-only checkbox or rejection reason input", () => {
    renderFilter();
    expect(screen.queryByText("Chỉ xem lead bị từ chối")).toBeNull();
    expect(screen.queryByPlaceholderText(/lý do từ chối/i)).toBeNull();
    expect(screen.queryByLabelText(/lý do từ chối/i)).toBeNull();
  });

  it("keeps the reengage date picker enabled and layout CSS-driven", () => {
    renderFilter();
    const rangePickers = screen.getAllByTestId("lead-reengage-range");
    expect(rangePickers).toHaveLength(2);
    for (const rangePicker of rangePickers) {
      expect(rangePicker).not.toHaveAttribute("disabled");
    }
    const trailingGroup = screen.getByTestId("lead-filter-trailing-group");
    expect(trailingGroup.getAttribute("style")).toBeNull();
    expect(trailingGroup.className).toBe("");
  });

  it("uses the responsive grid class without inline grid track overrides", () => {
    renderFilter();
    const grid = screen.getByTestId("rejected-filter-grid");
    expect(grid.className).toContain("crm-rejected-filter-grid");
    expect(grid.getAttribute("style") ?? "").not.toContain("grid-template-columns");
  });
});
