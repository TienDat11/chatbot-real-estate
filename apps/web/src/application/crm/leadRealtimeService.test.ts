import { describe, expect, it } from "vitest";
import { LeadRealtimeService } from "@/application/crm/leadRealtimeService";
import type { Lead } from "@/domain/crm/lead";
import type {
  LeadRepositoryPort,
  LeadStreamHandlers,
} from "@/domain/crm/ports/leadRepositoryPort";
import { TransportCapabilityNotSupportedError } from "@/domain/crm/ports/leadRepositoryPort";
import type {
  RealtimeConnectionState,
  RealtimeSubscriptionHandle,
} from "@/domain/realtime/realtimeSubscription";

// Replaceability proof: the service is driven through an in-memory repository
// implementing the SAME LeadRepositoryPort that FirestoreLeadRepository
// implements. There are ZERO firebase imports in this file — if the transport
// (Firestore -> Socket.IO) is swapped, domain/ and application/ change nothing
// and this test keeps passing against the new adapter.

/** Builds a minimal valid domain Lead fixture for tests. */
function makeLead(overrides: Partial<Lead> = {}): Lead {
  return {
    id: "lead-1",
    projectKey: "camellia",
    deviceId: "device-abc",
    name: "Nguyen Van A",
    maskedPhone: "090****456",
    note: null,
    budgetVnd: 2_500_000_000,
    consentFlags: { consentService: true, consentMarketing: false },
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

/**
 * In-memory transport simulating a realtime feed. Subscribes are served from a
 * local array; writes mutate the same array. Read-only adapters (e.g. a pure
 * WebSocket consumer) flip `readOnly` and then saveLead must reject.
 */
class InMemoryLeadRepository implements LeadRepositoryPort {
  private leadsByProject: Map<string, Lead[]> = new Map();
  private subscribers = new Set<() => void>();

  constructor(
    private readonly options: { readOnly: boolean },
    initialLeads: Lead[] = []
  ) {
    for (const lead of initialLeads) {
      this.pushLead(lead);
    }
  }

  private pushLead(lead: Lead): void {
    const projectLeads = this.leadsByProject.get(lead.projectKey) ?? [];
    this.leadsByProject.set(lead.projectKey, [...projectLeads, lead]);
    this.notifySubscribers();
  }

  private notifySubscribers(): void {
    for (const notify of this.subscribers) {
      notify();
    }
  }

  /** Emits a synthetic transport push as if a new lead arrived on the wire. */
  emitIncomingLead(lead: Lead): void {
    this.pushLead(lead);
  }

  streamLeadsByProject(
    request: { projectKey: string },
    handlers: LeadStreamHandlers
  ): RealtimeSubscriptionHandle {
    const deliver = () => {
      handlers.onConnectionStateChanged?.("active");
      handlers.onLeadsChanged(this.leadsByProject.get(request.projectKey) ?? []);
    };
    this.subscribers.add(deliver);
    handlers.onConnectionStateChanged?.("connecting");
    deliver();
    return {
      unsubscribe: () => this.subscribers.delete(deliver),
    };
  }

  streamLeadsForCrm(
    request: { assignedSalesFirebaseUid: string | null },
    handlers: LeadStreamHandlers
  ): RealtimeSubscriptionHandle {
    const deliver = () => {
      handlers.onConnectionStateChanged?.("active");
      const everyLead = [...this.leadsByProject.values()].flat();
      handlers.onLeadsChanged(
        request.assignedSalesFirebaseUid === null
          ? everyLead
          : everyLead.filter(
              (lead) =>
                lead.assignedSalesFirebaseUid === request.assignedSalesFirebaseUid
            )
      );
    };
    this.subscribers.add(deliver);
    handlers.onConnectionStateChanged?.("connecting");
    deliver();
    return {
      unsubscribe: () => this.subscribers.delete(deliver),
    };
  }

  async getLeadById(leadId: string): Promise<Lead | null> {
    for (const projectLeads of this.leadsByProject.values()) {
      const found = projectLeads.find((lead) => lead.id === leadId);
      if (found) {
        return found;
      }
    }
    return null;
  }

  async saveLead(lead: Lead): Promise<void> {
    if (this.options.readOnly) {
      throw new TransportCapabilityNotSupportedError("saveLead", "InMemoryReadOnly");
    }
    const projectLeads = this.leadsByProject.get(lead.projectKey) ?? [];
    this.leadsByProject.set(lead.projectKey, [
      ...projectLeads.filter((existing) => existing.id !== lead.id),
      lead,
    ]);
    this.notifySubscribers();
  }
}

describe("LeadRealtimeService", () => {
  it("streams domain leads for a project through the repository port", () => {
    const repository = new InMemoryLeadRepository(
      { readOnly: false },
      [makeLead({ id: "lead-a" }), makeLead({ id: "lead-b" })]
    );
    const service = new LeadRealtimeService(repository);

    const received: Lead[][] = [];
    const states: RealtimeConnectionState[] = [];
    const handle = service.streamLeadsByProject(
      { projectKey: "camellia" },
      {
        onLeadsChanged: (leads) => received.push(leads),
        onConnectionStateChanged: (state) => states.push(state),
      }
    );

    expect(states).toEqual(["connecting", "active"]);
    expect(received).toHaveLength(1);
    expect(received[0].map((lead) => lead.id).sort()).toEqual(["lead-a", "lead-b"]);
    expect(received[0][0]).toMatchObject({
      projectKey: "camellia",
      maskedPhone: "090****456",
      consentFlags: { consentService: true, consentMarketing: false },
    });
    handle.unsubscribe();
  });

  it("delivers transport pushes live while subscribed and stops after unsubscribe", () => {
    const repository = new InMemoryLeadRepository({ readOnly: false });
    const service = new LeadRealtimeService(repository);

    const received: Lead[][] = [];
    const handle = service.streamLeadsByProject(
      { projectKey: "camellia" },
      { onLeadsChanged: (leads) => received.push(leads) }
    );
    expect(received).toHaveLength(1);
    expect(received[0]).toEqual([]);

    repository.emitIncomingLead(makeLead({ id: "lead-live" }));
    expect(received).toHaveLength(2);
    expect(received[1].map((lead) => lead.id)).toEqual(["lead-live"]);

    handle.unsubscribe();
    repository.emitIncomingLead(makeLead({ id: "lead-after-unsub" }));
    expect(received).toHaveLength(2);
  });

  it("scopes the CRM stream to one sales' assignments; admins get everything", () => {
    const repository = new InMemoryLeadRepository({ readOnly: false }, [
      makeLead({ id: "lead-mine", assignedSalesFirebaseUid: "uid-sales-7" }),
      makeLead({ id: "lead-other", assignedSalesFirebaseUid: "uid-sales-8" }),
      makeLead({ id: "lead-unassigned" }),
    ]);
    const service = new LeadRealtimeService(repository);

    const salesReceived: Lead[][] = [];
    const salesHandle = service.streamLeadsForCrm(
      { assignedSalesFirebaseUid: "uid-sales-7" },
      { onLeadsChanged: (leads) => salesReceived.push(leads) }
    );
    expect(salesReceived.at(-1)?.map((lead) => lead.id)).toEqual(["lead-mine"]);

    const adminReceived: Lead[][] = [];
    const adminHandle = service.streamLeadsForCrm(
      { assignedSalesFirebaseUid: null },
      { onLeadsChanged: (leads) => adminReceived.push(leads) }
    );
    expect(adminReceived.at(-1)?.map((lead) => lead.id).sort()).toEqual([
      "lead-mine",
      "lead-other",
      "lead-unassigned",
    ]);

    salesHandle.unsubscribe();
    adminHandle.unsubscribe();
  });

  it("reads a single lead by opaque id through the repository port", async () => {
    const repository = new InMemoryLeadRepository(
      { readOnly: false },
      [makeLead({ id: "lead-a" })]
    );
    const service = new LeadRealtimeService(repository);

    const found = await service.getLeadById("lead-a");
    expect(found?.id).toBe("lead-a");

    const missing = await service.getLeadById("nope");
    expect(missing).toBeNull();
  });

  it("persists a lead through the repository port", async () => {
    const repository = new InMemoryLeadRepository({ readOnly: false });
    const service = new LeadRealtimeService(repository);
    const lead = makeLead({ id: "lead-new" });

    await service.saveLead(lead);
    expect(await service.getLeadById("lead-new")).toEqual(lead);
  });

  it("rejects writes on a read-only transport with TransportCapabilityNotSupportedError", async () => {
    const repository = new InMemoryLeadRepository({ readOnly: true });
    const service = new LeadRealtimeService(repository);

    await expect(service.saveLead(makeLead({ id: "lead-ro" }))).rejects.toBeInstanceOf(
      TransportCapabilityNotSupportedError
    );
    await expect(service.saveLead(makeLead({ id: "lead-ro" }))).rejects.toThrow(
      /does not support the "saveLead" capability/
    );
  });
});
