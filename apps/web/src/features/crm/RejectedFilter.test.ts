import { describe, expect, it } from "vitest";
import {
  EMPTY_LEAD_TOOLBAR_FILTER,
  filterLeadsByReengageWindow,
  filterLeadsByWorkflowStatus,
  selectCustomerLeadsByMaskedPhone,
} from "@/features/crm/crmLeadFilters";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";

describe("CRM lead toolbar filters", () => {
  const leads = [
    makeCrmLeadFixture({
      id: "before",
      workflowStatus: "new",
      reengageAt: "2026-09-09T23:59:59.000Z",
    }),
    makeCrmLeadFixture({
      id: "boundary",
      workflowStatus: "booked",
      reengageAt: "2026-09-10T03:00:00.000Z",
    }),
    makeCrmLeadFixture({
      id: "after",
      workflowStatus: "lost",
      reengageAt: "2026-09-11T03:00:00.000Z",
    }),
    makeCrmLeadFixture({ id: "unscheduled", workflowStatus: "lost", reengageAt: null }),
  ];

  it("keeps every lead when the reengage window is unbounded", () => {
    expect(filterLeadsByReengageWindow(leads, EMPTY_LEAD_TOOLBAR_FILTER)).toEqual(leads);
  });

  it("drops leads without reengage_at only after a window is requested", () => {
    expect(
      filterLeadsByReengageWindow(leads, {
        reengageWindowFromIsoDate: "2026-09-10",
        reengageWindowToIsoDate: null,
      }).map((lead) => lead.id)
    ).toEqual(["boundary", "after"]);
  });

  it("uses inclusive calendar-day boundaries for reengage_at", () => {
    expect(
      filterLeadsByReengageWindow(leads, {
        reengageWindowFromIsoDate: "2026-09-10",
        reengageWindowToIsoDate: "2026-09-10",
      }).map((lead) => lead.id)
    ).toEqual(["boundary"]);
  });

  it("keeps all statuses for null and filters exactly for a selected status", () => {
    expect(filterLeadsByWorkflowStatus(leads, null)).toEqual(leads);
    expect(filterLeadsByWorkflowStatus(leads, "lost").map((lead) => lead.id)).toEqual([
      "after",
      "unscheduled",
    ]);
  });

  it("composes status and date filtering with AND semantics", () => {
    const statusScoped = filterLeadsByWorkflowStatus(leads, "lost");
    expect(
      filterLeadsByReengageWindow(statusScoped, {
        reengageWindowFromIsoDate: "2026-09-10",
        reengageWindowToIsoDate: "2026-09-10",
      })
    ).toEqual([]);
  });
});

describe("selectCustomerLeadsByMaskedPhone", () => {
  it("groups leads sharing the anchor masked phone", () => {
    const anchor = makeCrmLeadFixture({ id: "anchor", maskedPhone: "090****456" });
    const sameCustomer = makeCrmLeadFixture({ id: "same", maskedPhone: "090****456" });
    const other = makeCrmLeadFixture({ id: "other", maskedPhone: "091****111" });
    expect(selectCustomerLeadsByMaskedPhone([anchor, sameCustomer, other], anchor).map((lead) => lead.id)).toEqual([
      "anchor",
      "same",
    ]);
  });

  it("falls back to only the anchor when its mask is unknown", () => {
    const anchor = makeCrmLeadFixture({ id: "anchor", maskedPhone: null });
    expect(selectCustomerLeadsByMaskedPhone([anchor, makeCrmLeadFixture()], anchor)).toEqual([anchor]);
  });
});
