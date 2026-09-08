/**
 * Regression coverage for the Firestore lead document -> domain Lead mapping,
 * focused on the numeric lead_id handoff: CustomerDetail's status/conversation
 * calls go through crmApiClient's integer-only guard, which accepts ONLY
 * domain Lead.leadId — a dropped or malformed mirror id would turn every CRM
 * drawer action into a client-side 400.
 */
import { describe, expect, it, vi } from "vitest";
import { mapLeadDocumentData } from "@/infrastructure/firebase/mappers/leadDocumentMapper";
import { makeCrmLeadFixture } from "@/features/crm/crmLeadFixture";

// Hermetic module boundary: the real firebase/firestore SDK is irrelevant to
// the pure shape translation under test, so stub the two symbols the mapper
// imports (the read path here only feeds ISO strings, never Timestamps).
vi.mock("firebase/firestore", () => {
  class FakeTimestamp {
    toDate(): Date {
      return new Date(0);
    }
  }
  return {
    Timestamp: FakeTimestamp,
    serverTimestamp: (): Record<string, never> => ({}),
  };
});

/** Full mirror-shaped document matching the shared CRM fixture defaults. */
function makeMirrorDocument(
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    project_key: "camellia",
    device_id: "device-abc",
    name: "Nguyen Van A",
    masked_phone: "090****456",
    note: null,
    budget_vnd: 2_500_000_000,
    consent_service: true,
    consent_marketing: true,
    lead_status: "new",
    status: "new",
    assigned_sales_id: null,
    assigned_sales_firebase_uid: null,
    rejection_reason: null,
    reengage_at: null,
    marketing_withdrawn_at: null,
    escal_count: 0,
    created_at: "2026-08-22T08:00:00.000Z",
    updated_at: "2026-08-22T08:00:00.000Z",
    closed_at: null,
    ...overrides,
  };
}

describe("mapLeadDocumentData — numeric lead_id mapping", () => {
  it("maps the mirrored numeric lead_id onto domain Lead.leadId", () => {
    const lead = mapLeadDocumentData(
      makeMirrorDocument({
        lead_id: 4213,
        lead_status: "assigned",
        status: "assigned",
      }) as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-1"
    );

    // Exact-domain-shape check: every field decodes, including the numeric id
    // and a non-fallback workflow status read from lead_status.
    expect(lead).toEqual(
      makeCrmLeadFixture({ id: "hmac-doc-1", leadId: 4213, workflowStatus: "assigned" })
    );
    expect(lead.leadId).toBe(4213);
  });

  it("keeps leadId undefined on documents written before the field existed", () => {
    // The default mirror document carries no lead_id, exactly like rows
    // mirrored before the Postgres id field was introduced.
    const lead = mapLeadDocumentData(
      makeMirrorDocument() as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-2"
    );

    // The optional id stays absent (not coerced to 0/null) so the crmApiClient
    // guard — not some silent wrong endpoint — handles the legacy document.
    expect(lead.leadId).toBeUndefined();
    expect(lead).toEqual(makeCrmLeadFixture({ id: "hmac-doc-2" }));
  });

  it.each([null, "4213", 0, -7, 12.5])(
    "decodes malformed lead_id %p as undefined instead of leaking it",
    (malformedLeadId) => {
      const lead = mapLeadDocumentData(
        makeMirrorDocument({ lead_id: malformedLeadId }) as Parameters<
          typeof mapLeadDocumentData
        >[0],
        "hmac-doc-3"
      );

      // Same safe-positive-integer contract as crmApiClient.leadEndpointId.
      expect(lead.leadId).toBeUndefined();
    }
  );
});

describe("mapLeadDocumentData — customer_id mapping", () => {
  it("maps the mirrored customer_id field onto domain Lead.customerId", () => {
    const lead = mapLeadDocumentData(
      makeMirrorDocument({ customer_id: "hmac-customer-9" }) as Parameters<
        typeof mapLeadDocumentData
      >[0],
      "hmac-doc-10"
    );

    // The reveal/consent routes key by this identity; the per-lead document id
    // stopped being the customer digest at ADR-0004.
    expect(lead.customerId).toBe("hmac-customer-9");
  });

  it("keeps customerId undefined on documents written before the field existed", () => {
    const lead = mapLeadDocumentData(
      makeMirrorDocument() as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-11"
    );

    // Legacy documents: the document id itself was the customer digest, so the
    // drawer falls back to Lead.id instead of a fabricated identity.
    expect(lead.customerId).toBeUndefined();
  });

  it.each([null, "", 42, {}])(
    "decodes malformed customer_id %p as undefined instead of leaking it",
    (malformedCustomerId) => {
      const lead = mapLeadDocumentData(
        makeMirrorDocument({ customer_id: malformedCustomerId }) as Parameters<
          typeof mapLeadDocumentData
        >[0],
        "hmac-doc-12"
      );

      expect(lead.customerId).toBeUndefined();
    }
  );
});

describe("mapLeadDocumentData — display_name / legacy name mapping", () => {
  it("maps a display_name-only document onto domain Lead.name", () => {
    const lead = mapLeadDocumentData(
      makeMirrorDocument({ display_name: "Synthetic Lead One", name: null }) as Parameters<
        typeof mapLeadDocumentData
      >[0],
      "hmac-doc-4"
    );

    expect(lead.name).toBe("Synthetic Lead One");
  });

  it("still maps legacy documents that carry only the old name field", () => {
    const lead = mapLeadDocumentData(
      // No display_name key at all, like every document mirrored before the
      // backend introduced the column.
      makeMirrorDocument() as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-5"
    );

    expect(lead.name).toBe("Nguyen Van A");
  });

  it("prefers display_name over the legacy name when both are present", () => {
    const lead = mapLeadDocumentData(
      makeMirrorDocument({
        display_name: "Synthetic Lead Two",
        name: "Legacy Fallback Name",
      }) as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-6"
    );

    expect(lead.name).toBe("Synthetic Lead Two");
  });

  it("keeps name null when both fields are null or absent", () => {
    const bothNull = mapLeadDocumentData(
      makeMirrorDocument({
        display_name: null,
        name: null,
      }) as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-7"
    );
    const neitherDocument = makeMirrorDocument();
    delete neitherDocument.name;
    delete neitherDocument.display_name;
    const neitherPresent = mapLeadDocumentData(
      neitherDocument as Parameters<typeof mapLeadDocumentData>[0],
      "hmac-doc-8"
    );

    expect(bothNull.name).toBeNull();
    expect(neitherPresent.name).toBeNull();
  });

  it("treats an empty-string display_name like any other string value", () => {
    // Documented convention parity: same `??` decode as every other nullable
    // string field — only null/absent falls through, "" is preserved verbatim.
    const lead = mapLeadDocumentData(
      makeMirrorDocument({ display_name: "", name: "Legacy Fallback Name" }) as Parameters<
        typeof mapLeadDocumentData
      >[0],
      "hmac-doc-9"
    );

    expect(lead.name).toBe("");
  });
});
