import { describe, expect, it } from "vitest";
import {
  EMPTY_REJECTED_LEADS_FILTER,
  filterLeadsByWorkflowStatus,
  filterRejectedLeads,
  matchesRejectedLeadsFilter,
  selectCustomerLeadsByMaskedPhone,
} from "@/features/crm/crmLeadFilters";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";

const rejectedLead = makeCrmLeadFixture({
  id: "lead-rejected",
  workflowStatus: "lost",
  rejectionReason: "Khách không có nhu cầu",
  reengageAt: "2026-09-10T03:00:00.000Z",
  projectKey: "camellia",
});

describe("filterRejectedLeads", () => {
  it("keeps every lost lead when no criterion is set — with or without a stored reason", () => {
    const leads = [
      rejectedLead,
      makeCrmLeadFixture({ id: "lead-new", workflowStatus: "new" }),
      makeCrmLeadFixture({ id: "lead-lost", workflowStatus: "lost" }),
    ];
    expect(filterRejectedLeads(leads, EMPTY_REJECTED_LEADS_FILTER).map((l) => l.id)).toEqual([
      "lead-rejected",
      "lead-lost",
    ]);
  });

  it("matches the project key exactly", () => {
    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        projectKey: "camellia",
      })
    ).toHaveLength(1);
    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        projectKey: "soleil",
      })
    ).toHaveLength(0);
  });

  it("matches the rejection reason as a case-insensitive substring", () => {
    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        rejectionReason: "KHÔNG CÓ NHU CẦU",
      })
    ).toHaveLength(1);
    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        rejectionReason: "giá cao",
      })
    ).toHaveLength(0);
  });

  it("treats a blank rejection reason as no filter", () => {
    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        rejectionReason: "   ",
      })
    ).toHaveLength(1);
  });

  it("applies the reengage window inclusively on the calendar day", () => {
    const aroundWindow = filterRejectedLeads([rejectedLead], {
      ...EMPTY_REJECTED_LEADS_FILTER,
      reengageWindowFromIsoDate: "2026-09-10",
      reengageWindowToIsoDate: "2026-09-10",
    });
    expect(aroundWindow).toHaveLength(1);

    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        reengageWindowFromIsoDate: "2026-09-11",
      })
    ).toHaveLength(0);
    expect(
      filterRejectedLeads([rejectedLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        reengageWindowToIsoDate: "2026-09-09",
      })
    ).toHaveLength(0);
  });

  it("drops rejected leads without a reengage date once a window is requested", () => {
    const noReengageLead = makeCrmLeadFixture({
      id: "lead-no-reengage",
      workflowStatus: "lost",
      rejectionReason: "Sai nhu cầu",
      reengageAt: null,
    });
    expect(
      filterRejectedLeads([noReengageLead], {
        ...EMPTY_REJECTED_LEADS_FILTER,
        reengageWindowFromIsoDate: "2026-09-01",
      })
    ).toHaveLength(0);
  });

  it("matchesRejectedLeadsFilter composes every criterion with AND", () => {
    expect(
      matchesRejectedLeadsFilter(rejectedLead, {
        projectKey: "camellia",
        rejectionReason: "nhu cầu",
        reengageWindowFromIsoDate: "2026-09-01",
        reengageWindowToIsoDate: "2026-09-30",
      })
    ).toBe(true);
    expect(
      matchesRejectedLeadsFilter(rejectedLead, {
        ...EMPTY_REJECTED_LEADS_FILTER,
        rejectionReason: "nhu cầu",
        projectKey: "soleil",
      })
    ).toBe(false);
  });
});

describe("filterLeadsByWorkflowStatus", () => {
  it("keeps everything for a null status and filters exactly otherwise", () => {
    const leads = [
      makeCrmLeadFixture({ id: "a", workflowStatus: "new" }),
      makeCrmLeadFixture({ id: "b", workflowStatus: "booked" }),
    ];
    expect(filterLeadsByWorkflowStatus(leads, null)).toHaveLength(2);
    expect(
      filterLeadsByWorkflowStatus(leads, "booked").map((lead) => lead.id)
    ).toEqual(["b"]);
  });
});

describe("selectCustomerLeadsByMaskedPhone", () => {
  it("groups leads sharing the anchor masked phone", () => {
    const anchor = makeCrmLeadFixture({ id: "anchor", maskedPhone: "090****456" });
    const sameCustomer = makeCrmLeadFixture({
      id: "same",
      projectKey: "soleil",
      maskedPhone: "090****456",
    });
    const other = makeCrmLeadFixture({ id: "other", maskedPhone: "091****111" });
    expect(
      selectCustomerLeadsByMaskedPhone([anchor, sameCustomer, other], anchor).map(
        (lead) => lead.id
      )
    ).toEqual(["anchor", "same"]);
  });

  it("falls back to only the anchor when its mask is unknown", () => {
    const anchor = makeCrmLeadFixture({ id: "anchor", maskedPhone: null });
    const other = makeCrmLeadFixture({ id: "other", maskedPhone: "091****111" });
    expect(selectCustomerLeadsByMaskedPhone([anchor, other], anchor)).toEqual([anchor]);
  });
});
