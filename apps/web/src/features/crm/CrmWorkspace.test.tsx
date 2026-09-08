// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Lead } from "@/domain/crm/lead";
import { makeCrmLeadFixture } from "./crmLeadFixture";

const leads: Lead[] = [
  makeCrmLeadFixture({
    id: "opaque-lead-101",
    leadId: 101,
    name: "Owned lead",
    assignedSalesFirebaseUid: "sales-1",
  }),
  makeCrmLeadFixture({
    id: "opaque-lead-202",
    leadId: 202,
    name: "Other sales lead",
    assignedSalesFirebaseUid: "sales-2",
  }),
];

const leadStream = {
  leads,
  connectionState: "active" as const,
  error: null,
  applyOptimisticLeadPatch: vi.fn(),
  clearOptimisticLeadPatch: vi.fn(),
};

vi.mock("@/lib/AuthProvider", () => ({
  useAuth: () => ({ user: { uid: "sales-1", role: "sales" }, loading: false }),
}));
vi.mock("@/lib/realtime/useRealtimeContainer", () => ({
  useRealtimeContainer: () => ({
    crmNoteStore: { readLeadNote: vi.fn(async () => null), saveLeadNote: vi.fn() },
  }),
}));
vi.mock("@/infrastructure/firebase/firebaseAuthenticationService", () => ({
  getFreshIdToken: vi.fn(async () => "fresh-token"),
}));
vi.mock("@/features/chat/activeProjects", () => ({
  loadActiveProjects: vi.fn(async () => []),
  projectDisplayName: (project: { project_key: string }) => project.project_key,
}));
vi.mock("./useCrmLeadStream", () => ({
  useSharedCrmLeadStream: () => leadStream,
}));
// The table's rows come from the server-paged REST listing; the workspace test
// only exercises deep-link selection, so the page source is stubbed here
// (the paged contract itself is covered by useCrmLeadsPage.test.tsx).
vi.mock("./crmApiClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./crmApiClient")>()),
  fetchCrmLeadsPage: vi.fn(async () => ({
    rows: [],
    nextCursor: null,
    hasMore: false,
    serverTime: "2026-09-03T00:00:00Z",
  })),
}));
vi.mock("./RejectedFilter", () => ({ RejectedFilter: () => null }));
vi.mock("./LeadTable", () => ({
  LeadTable: ({ rows }: { rows: readonly Lead[] }) => (
    <div>
      {rows.map((lead) => (
        <div key={lead.id}>{lead.name}</div>
      ))}
    </div>
  ),
}));
vi.mock("./CustomerDetail", () => ({
  CustomerDetail: ({ open, anchorLead }: { open: boolean; anchorLead: Lead | null }) =>
    open ? <div role="dialog">Customer detail: {anchorLead?.name}</div> : null,
}));

import { CrmWorkspace } from "./CrmWorkspace";
import { getFreshIdToken } from "@/infrastructure/firebase/firebaseAuthenticationService";

beforeEach(() => {
  window.history.replaceState({}, "", "/sales/leads");
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CrmWorkspace lead deep-link regression", () => {
  it("renders normally when the bearer mint rejects (signed-out race)", async () => {
    vi.mocked(getFreshIdToken).mockRejectedValueOnce(new Error("signed out"));

    render(<CrmWorkspace />);

    await act(async () => {});
    expect(screen.getByText(/Bạn đang xem/)).toBeTruthy();
  });
  it("opens the matching owned decimal lead without a second row click", async () => {
    window.history.replaceState({}, "", "/sales/leads?lead=101");

    render(<CrmWorkspace />);

    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "Customer detail: Owned lead"
    );
  });

  it.each([
    ["missing query", "/sales/leads"],
    ["invalid id", "/sales/leads?lead=not-a-decimal"],
    ["non-owned id", "/sales/leads?lead=202"],
  ])("keeps CustomerDetail closed for %s", async (_caseName, url) => {
    window.history.replaceState({}, "", url);

    render(<CrmWorkspace />);

    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("applies query updates and browser back without reopening a stale lead", async () => {
    window.history.replaceState({}, "", "/sales/leads?lead=101");
    render(<CrmWorkspace />);
    expect(await screen.findByRole("dialog")).toHaveTextContent("Owned lead");

    act(() => {
      window.history.pushState({}, "", "/sales/leads?lead=202");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.queryByRole("dialog")).toBeNull();

    act(() => {
      window.history.replaceState({}, "", "/sales/leads?lead=101");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("dialog")).toHaveTextContent("Owned lead");
  });
});
