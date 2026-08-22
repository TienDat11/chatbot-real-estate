// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";
import { LeadTable } from "@/features/crm/LeadTable";

afterEach(() => {
  cleanup();
});

const leads = [
  makeCrmLeadFixture({
    id: "lead-1",
    name: "Nguyen Van A",
    maskedPhone: "090****456",
    projectKey: "camellia",
    workflowStatus: "new",
  }),
  makeCrmLeadFixture({
    id: "lead-2",
    name: "Tran Thi B",
    maskedPhone: "091****111",
    projectKey: "soleil",
    workflowStatus: "lost",
    rejectionReason: "Khách không có nhu cầu",
  }),
];

function renderLeadTable() {
  const onOpenCustomerDetail = vi.fn();
  render(
    <LeadTable
      leads={leads}
      connectionState="active"
      onOpenCustomerDetail={onOpenCustomerDetail}
    />
  );
  return { onOpenCustomerDetail };
}

describe("LeadTable", () => {
  it("renders one row per realtime lead with Vietnamese status labels", async () => {
    renderLeadTable();
    expect(await screen.findByText("Nguyen Van A")).toBeTruthy();
    expect(screen.getByText("090****456")).toBeTruthy();
    expect(screen.getByText("Tran Thi B")).toBeTruthy();
    expect(screen.getByText("Khách mới")).toBeTruthy();
    expect(screen.getByText("Từ chối")).toBeTruthy();
    expect(screen.getByText("Khách không có nhu cầu")).toBeTruthy();
    expect(screen.getByText("Trực tiếp")).toBeTruthy();
  });

  it("opens the customer detail anchored on the clicked lead", () => {
    const { onOpenCustomerDetail } = renderLeadTable();
    fireEvent.click(screen.getAllByRole("button", { name: "Chi tiết" })[1]);
    expect(onOpenCustomerDetail).toHaveBeenCalledWith("lead-2");
  });

  it("requests notification permission only from the opt-in button gesture", async () => {
    const requestPermission = vi.fn(async () => "granted" as NotificationPermission);
    // A class (typeof === "function") matches the real Notification API shape.
    class NotificationStub {
      static permission = "default" as NotificationPermission;
      static requestPermission = requestPermission;
    }
    vi.stubGlobal("Notification", NotificationStub);
    renderLeadTable();

    fireEvent.click(screen.getByRole("button", { name: "Bật thông báo" }));
    expect(requestPermission).toHaveBeenCalledTimes(1);
    // Once granted the opt-in disappears — no further permission prompts.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Bật thông báo" })).toBeNull()
    );
    vi.unstubAllGlobals();
  });

  it("hides the opt-in when the browser cannot show notifications", () => {
    renderLeadTable();
    expect(screen.queryByRole("button", { name: "Bật thông báo" })).toBeNull();
  });
});
