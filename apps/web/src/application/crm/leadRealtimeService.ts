/**
 * LeadRealtimeService — use-case service for realtime lead streaming.
 *
 * Application layer: depends ONLY on the LeadRepositoryPort outbound port via
 * constructor injection. It has no firebase imports, no React imports, and no
 * knowledge of any concrete transport. The composition root (infrastructure/
 * realtimeContainer.ts) wires the concrete repository into this service.
 */
import type { Lead } from "@/domain/crm/lead";
import type {
  LeadRepositoryPort,
  LeadStreamHandlers,
} from "@/domain/crm/ports/leadRepositoryPort";
import type { RealtimeSubscriptionHandle } from "@/domain/realtime/realtimeSubscription";

export class LeadRealtimeService {
  constructor(private readonly leadRepository: LeadRepositoryPort) {}

  /**
   * Subscribes to the live lead stream for a project. Handlers receive DOMAIN
   * Lead entities; the caller owns the returned handle's lifecycle.
   */
  streamLeadsByProject(
    request: { projectKey: string },
    handlers: LeadStreamHandlers
  ): RealtimeSubscriptionHandle {
    return this.leadRepository.streamLeadsByProject(request, handlers);
  }

  /** One-shot read of a single lead by its opaque document id. */
  getLeadById(leadId: string): Promise<Lead | null> {
    return this.leadRepository.getLeadById(leadId);
  }

  /** Persists a lead; read-only transports reject with a capability error. */
  saveLead(lead: Lead): Promise<void> {
    return this.leadRepository.saveLead(lead);
  }
}
