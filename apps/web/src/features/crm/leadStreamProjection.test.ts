import { describe, expect, it } from "vitest";
import {
  collectIncomingLeadIds,
  mergeLeadsWithOptimisticPatches,
  selectNotifiableIncomingLeads,
  type OptimisticLeadPatch,
} from "@/features/crm/leadStreamProjection";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";

/** Map literal helper so status literals keep their domain union types. */
function patchMap(entries: [string, OptimisticLeadPatch][]) {
  return new Map<string, OptimisticLeadPatch>(entries);
}

describe("mergeLeadsWithOptimisticPatches", () => {
  it("applies an in-flight patch over the snapshot", () => {
    const lead = makeCrmLeadFixture({ id: "lead-1", workflowStatus: "new" });
    const patches = patchMap([
      ["lead-1", { workflowStatus: "lost", rejectionReason: "Sai nhu cầu" }],
    ]);
    const { leads, caughtUpLeadIds } = mergeLeadsWithOptimisticPatches([lead], patches);
    expect(leads[0]).toMatchObject({
      id: "lead-1",
      workflowStatus: "lost",
      rejectionReason: "Sai nhu cầu",
    });
    expect(caughtUpLeadIds).toEqual([]);
  });

  it("releases the patch once the snapshot status caught up", () => {
    const caughtUpLead = makeCrmLeadFixture({
      id: "lead-1",
      workflowStatus: "lost",
    });
    const patches = patchMap([["lead-1", { workflowStatus: "lost" }]]);
    const { leads, caughtUpLeadIds } = mergeLeadsWithOptimisticPatches(
      [caughtUpLead],
      patches
    );
    expect(caughtUpLeadIds).toEqual(["lead-1"]);
    expect(leads[0]).toEqual(caughtUpLead);
  });

  it("keeps the patch while the snapshot still disagrees", () => {
    const staleLead = makeCrmLeadFixture({ id: "lead-1", workflowStatus: "new" });
    const patches = patchMap([["lead-1", { workflowStatus: "called" }]]);
    const { leads, caughtUpLeadIds } = mergeLeadsWithOptimisticPatches([staleLead], patches);
    expect(leads[0].workflowStatus).toBe("called");
    expect(caughtUpLeadIds).toEqual([]);
  });
});

describe("collectIncomingLeadIds", () => {
  it("returns ids absent from the previous snapshot", () => {
    const previous = new Set(["lead-1"]);
    const leads = [
      makeCrmLeadFixture({ id: "lead-1" }),
      makeCrmLeadFixture({ id: "lead-2" }),
    ];
    expect(collectIncomingLeadIds(previous, leads)).toEqual(["lead-2"]);
  });
});

describe("selectNotifiableIncomingLeads", () => {
  it("notifies only incoming leads that are still untouched (status new)", () => {
    const leads = [
      makeCrmLeadFixture({ id: "lead-new", workflowStatus: "new" }),
      makeCrmLeadFixture({ id: "lead-assigned", workflowStatus: "assigned" }),
      makeCrmLeadFixture({ id: "lead-known", workflowStatus: "new" }),
    ];
    const notifiable = selectNotifiableIncomingLeads(leads, [
      "lead-new",
      "lead-assigned",
    ]);
    expect(notifiable.map((lead) => lead.id)).toEqual(["lead-new"]);
  });
});
