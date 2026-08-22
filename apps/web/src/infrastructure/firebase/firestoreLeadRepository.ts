/**
 * FirestoreLeadRepository — Firestore adapter implementing LeadRepositoryPort.
 *
 * Streaming delegates to FirestoreRealtimeChannel (the generic transport
 * adapter); point reads and writes talk to the `leads` collection directly.
 * All Firestore data flows through leadDocumentMapper, so the repository only
 * ever produces/consumes DOMAIN Lead entities.
 */
import { doc, getDoc, setDoc, type Firestore } from "firebase/firestore";
import type { Lead } from "@/domain/crm/lead";
import type {
  LeadRepositoryPort,
  LeadStreamHandlers,
} from "@/domain/crm/ports/leadRepositoryPort";
import type { RealtimeChannelPort } from "@/domain/realtime/ports/realtimeChannelPort";
import type { RealtimeSubscriptionHandle } from "@/domain/realtime/realtimeSubscription";
import { leadToLeadDocumentDto, mapLeadDocumentData } from "./mappers/leadDocumentMapper";

/** Firestore collection name mirroring the backend Postgres leads table. */
export const LEAD_COLLECTION_NAME = "leads";

/** Result cap for a per-project lead stream (keeps initial snapshots light). */
export const LEAD_STREAM_LIMIT = 100;

export class FirestoreLeadRepository implements LeadRepositoryPort {
  constructor(
    private readonly realtimeChannel: RealtimeChannelPort<Lead>,
    private readonly firestore: Firestore
  ) {}

  streamLeadsByProject(
    request: { projectKey: string },
    handlers: LeadStreamHandlers
  ): RealtimeSubscriptionHandle {
    // where(project_key ==) + orderBy(created_at desc) requires the composite
    // index declared in apps/web/firestore.indexes.json.
    return this.realtimeChannel.subscribeToQuery(
      {
        collectionName: LEAD_COLLECTION_NAME,
        filters: [
          { fieldPath: "project_key", operator: "==", value: request.projectKey },
        ],
        orderBy: { fieldPath: "created_at", direction: "desc" },
        limit: LEAD_STREAM_LIMIT,
      },
      {
        onDocumentsChanged: (leads) => handlers.onLeadsChanged(leads),
        onError: handlers.onError,
        onConnectionStateChanged: handlers.onConnectionStateChanged,
      }
    );
  }

  async getLeadById(leadId: string): Promise<Lead | null> {
    const documentSnapshot = await getDoc(
      doc(this.firestore, LEAD_COLLECTION_NAME, leadId)
    );
    if (!documentSnapshot.exists()) {
      return null;
    }
    return mapLeadDocumentData(documentSnapshot.data(), documentSnapshot.id);
  }

  async saveLead(lead: Lead): Promise<void> {
    await setDoc(
      doc(this.firestore, LEAD_COLLECTION_NAME, lead.id),
      leadToLeadDocumentDto(lead),
      { merge: true }
    );
  }
}
