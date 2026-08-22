/**
 * Domain Lead fixture factory for CRM tests (story 9.3). Co-located with the
 * feature so every crm test file shares one canonical shape.
 */
import type { Lead } from "@/domain/crm/lead";

/** Builds a minimal valid domain Lead; every field overridable. */
export function makeCrmLeadFixture(overrides: Partial<Lead> = {}): Lead {
  return {
    id: "lead-1",
    projectKey: "camellia",
    deviceId: "device-abc",
    name: "Nguyen Van A",
    maskedPhone: "090****456",
    note: null,
    budgetVnd: 2_500_000_000,
    consentFlags: { consentService: true, consentMarketing: true },
    workflowStatus: "new",
    assignedSalesId: null,
    assignedSalesFirebaseUid: null,
    rejectionReason: null,
    reengageAt: null,
    marketingWithdrawnAt: null,
    escalCount: 0,
    createdAt: "2026-08-22T08:00:00.000Z",
    updatedAt: "2026-08-22T08:00:00.000Z",
    closedAt: null,
    ...overrides,
  };
}
