// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";
import { LeadTable } from "@/features/crm/LeadTable";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

/**
 * The vitest.setup.ts matchMedia polyfill always reports "no match", which
 * Grid.useBreakpoint reads as a compact viewport. Stub every min-width query
 * as matching to render the full desktop column set.
 */
function stubWideViewport() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("min-width"),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

/**
 * Tablet-grade viewport: min-width queries at or below `maxMatchingWidth`
 * match (md=768 matches, lg=992 does not at ~820px). Distinguishes the
 * tablet column plan from both the mobile and desktop ones.
 */
function stubViewportWithWidth(maxMatchingWidth: number) {
  vi.stubGlobal("matchMedia", (query: string) => {
    const widthMatch = /min-width:\s*(\d+)px/.exec(query);
    const width = widthMatch !== null ? Number(widthMatch[1]) : Number.POSITIVE_INFINITY;
    return {
      matches: width <= maxMatchingWidth,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    };
  });
}

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

/** Default server-paged props; every test overrides what it exercises. */
function basePagingProps() {
  return {
    isLoading: false,
    pageIndex: 0,
    pageSize: 20,
    hasNextPage: false,
    hasPreviousPage: false,
    onPageChange: vi.fn(),
    onPageSizeChange: vi.fn(),
  };
}

function renderLeadTable(props: Partial<Parameters<typeof LeadTable>[0]> = {}) {
  const onOpenCustomerDetail = vi.fn();
  const paging = basePagingProps();
  render(
    <LeadTable
      rows={leads}
      connectionState="active"
      onOpenCustomerDetail={onOpenCustomerDetail}
      {...paging}
      {...props}
    />
  );
  return { onOpenCustomerDetail, paging };
}

describe("LeadTable", () => {
  it("renders one row per realtime lead with Vietnamese status labels", async () => {
    stubWideViewport();
    renderLeadTable();
    expect(await screen.findByText("Nguyen Van A")).toBeTruthy();
    expect(screen.getByText("090****456")).toBeTruthy();
    expect(screen.getByText("Tran Thi B")).toBeTruthy();
    expect(screen.getByText("Khách mới")).toBeTruthy();
    expect(screen.getByText("Từ chối")).toBeTruthy();
    expect(screen.getByText("Khách không có nhu cầu")).toBeTruthy();
    expect(screen.getByText("Trực tiếp")).toBeTruthy();
  });

  it("keeps identity, status and the detail action on a compact viewport", () => {
    // Default jsdom matchMedia reports every breakpoint as not matching.
    renderLeadTable();
    expect(screen.getByText("Nguyen Van A")).toBeTruthy();
    expect(screen.getByText("Khách mới")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Chi tiết" })).toHaveLength(2);
    // The widest columns are intentionally hidden instead of overflowing.
    expect(screen.queryByText("Ngân sách (VNĐ)")).toBeNull();
    expect(screen.queryByText("Lý do từ chối")).toBeNull();
    expect(screen.queryByText("Hẹn gọi lại")).toBeNull();
    expect(screen.queryByText("Cập nhật")).toBeNull();
  });

  it("shows the full column set on a wide viewport", () => {
    stubWideViewport();
    renderLeadTable();
    // antd renders a hidden measure cell per column, so use *AllBy queries.
    expect(screen.getAllByText("Ngân sách (VNĐ)").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Lý do từ chối").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Hẹn gọi lại").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Cập nhật").length).toBeGreaterThan(0);
  });

  it("uses the intermediate tablet column plan between md and lg", () => {
    // ~820px viewport: md matches, lg does not — numeric/date columns stay,
    // the widest free-width rejection column is intentionally dropped.
    stubViewportWithWidth(820);
    renderLeadTable();
    expect(screen.getAllByText("Ngân sách (VNĐ)").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Hẹn gọi lại").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Cập nhật").length).toBeGreaterThan(0);
    expect(screen.queryByText("Lý do từ chối")).toBeNull();
    // Identity, status and the action remain reachable.
    expect(screen.getByText("Nguyen Van A")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Chi tiết" })).toHaveLength(2);
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

  it("persists a denied choice and explains how to recover", async () => {
    const requestPermission = vi.fn(async () => "denied" as NotificationPermission);
    class NotificationStub {
      static permission = "default" as NotificationPermission;
      static requestPermission = requestPermission;
    }
    vi.stubGlobal("Notification", NotificationStub);
    renderLeadTable();

    fireEvent.click(screen.getByRole("button", { name: "Bật thông báo" }));
    await waitFor(() =>
      expect(screen.getByTestId("notification-denied-help")).toBeTruthy()
    );
    expect(window.localStorage.getItem("crm.browser-notifications.preference")).toBe("denied");
    expect(screen.queryByRole("button", { name: "Bật thông báo" })).toBeNull();
    vi.unstubAllGlobals();
  });

  it("does not ask again when a denied preference is persisted", () => {
    window.localStorage.setItem("crm.browser-notifications.preference", "denied");
    const requestPermission = vi.fn(async () => "default" as NotificationPermission);
    class NotificationStub {
      static permission = "default" as NotificationPermission;
      static requestPermission = requestPermission;
    }
    vi.stubGlobal("Notification", NotificationStub);
    renderLeadTable();
    expect(screen.queryByRole("button", { name: "Bật thông báo" })).toBeNull();
    expect(screen.getByTestId("notification-denied-help")).toBeTruthy();
    expect(requestPermission).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("hides the opt-in when the browser cannot show notifications", () => {
    renderLeadTable();
    expect(screen.queryByRole("button", { name: "Bật thông báo" })).toBeNull();
  });

  it("requests server pages by cursor, never slicing the current rows locally", async () => {
    // Forward: on page 0 with has_more the stable total (rows.length + pageSize)
    // offers exactly one page beyond the current one, never a phantom page that
    // maps to an unprimed cursor.
    const forward = renderLeadTable({ pageIndex: 0, pageSize: 20, hasNextPage: true, hasPreviousPage: false });
    expect(screen.getByTitle("2")).toBeTruthy();
    expect(screen.queryByTitle("3")).toBeNull();
    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(forward.paging.onPageChange).toHaveBeenCalledWith(1));

    // Backward: on page 1 the pager offers page 1 (the visited start), and
    // clicking it requests that page from the server by cursor.
    cleanup();
    const backward = renderLeadTable({ pageIndex: 1, pageSize: 20, hasNextPage: true, hasPreviousPage: true });
    fireEvent.click(screen.getByTitle("1"));
    await waitFor(() => expect(backward.paging.onPageChange).toHaveBeenCalledWith(0));
  });

  it("changes page size through the server (re-queried from page 0)", async () => {
    const { paging } = renderLeadTable({ pageIndex: 2, pageSize: 20, hasNextPage: true });
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(await screen.findByText("50 / page"));
    await waitFor(() => expect(paging.onPageSizeChange).toHaveBeenCalledWith(50));
  });
});
