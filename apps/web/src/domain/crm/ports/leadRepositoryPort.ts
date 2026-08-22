/**
 * LeadRepositoryPort — the CRM-facing persistence/realtime contract for leads.
 *
 * OUTBOUND port: application services depend on this interface (never on a
 * concrete repository), so the realtime source can be swapped per transport
 * without touching the use-case logic. Firestore implements it today; a
 * Socket.IO implementation would implement the same methods.
 */
import type { Lead } from "@/domain/crm/lead";
import type {
  RealtimeChannelError,
  RealtimeConnectionState,
  RealtimeSubscriptionHandle,
} from "@/domain/realtime/realtimeSubscription";

/** Handlers for a live lead stream (per project). */
export interface LeadStreamHandlers {
  /** Full current lead set for the subscribed project on every change. */
  onLeadsChanged(leads: Lead[]): void;
  onError?(error: RealtimeChannelError): void;
  onConnectionStateChanged?(state: RealtimeConnectionState): void;
}

/** Request for the CRM-wide lead stream (story 9.3). */
export interface CrmLeadStreamRequest {
  /**
   * Firebase uid restricting the stream to one sales' assignments. Null means
   * the caller is an admin and may see every lead — Firestore rules enforce
   * the same decision server-side.
   */
  assignedSalesFirebaseUid: string | null;
}

/** Contract for reading and writing leads through any transport. */
export interface LeadRepositoryPort {
  /**
   * Subscribes to every lead of one project and streams DOMAIN Lead entities.
   * The returned handle MUST be unsubscribed when the consumer unmounts.
   */
  streamLeadsByProject(
    request: { projectKey: string },
    handlers: LeadStreamHandlers
  ): RealtimeSubscriptionHandle;

  /**
   * Subscribes to the CRM lead table (story 9.3): every lead for an admin,
   * or only the leads assigned to one sales. The returned handle MUST be
   * unsubscribed when the consumer unmounts.
   */
  streamLeadsForCrm(
    request: CrmLeadStreamRequest,
    handlers: LeadStreamHandlers
  ): RealtimeSubscriptionHandle;

  /** Fetches one lead by its opaque document id, or null when absent. */
  getLeadById(leadId: string): Promise<Lead | null>;

  /**
   * Persists a lead. Read-only transports (e.g. a pure WebSocket consumer)
   * MUST reject this with TransportCapabilityNotSupportedError instead of
   * silently dropping the write.
   */
  saveLead(lead: Lead): Promise<void>;
}

/**
 * Raised when a transport cannot perform a capability the port contract
 * allows (for example a read-only realtime adapter asked to saveLead).
 */
export class TransportCapabilityNotSupportedError extends Error {
  constructor(capabilityName: string, transportName: string) {
    super(
      `Transport "${transportName}" does not support the "${capabilityName}" capability.`
    );
    this.name = "TransportCapabilityNotSupportedError";
  }
}
