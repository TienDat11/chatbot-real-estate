/**
 * REALTIME_CONTAINER — composition root (the single wiring point).
 *
 * This is THE file to edit when swapping the realtime transport (Firestore
 * today, WebSocket/Socket.IO later). It wires the concrete adapters into the
 * application service, all typed by the PORT interfaces so nothing above this
 * layer knows what transport is wired underneath.
 *
 * WHY the wiring is lazy: wave 2 mounts RealtimeProvider in layout.tsx, so
 * this module is also evaluated during a Next.js server render. Initializing
 * Firebase there would read NEXT_PUBLIC_FIREBASE_* and construct a Firestore
 * client in a Node context, which is not the environment Firestore expects.
 * Every firebase access is therefore deferred until the first subscribe/save
 * call — which only ever happens from a browser useEffect. Reading properties
 * off the container (e.g. grabbing the service reference in a hook render) is
 * always SSR-safe; the exported REALTIME_CONTAINER keeps its port typing, so
 * consumers do not change.
 *
 * Swap walkthrough: see apps/web/src/lib/realtime/README.md.
 */
import { LeadRealtimeService } from "@/application/crm/leadRealtimeService";
import type { Lead } from "@/domain/crm/lead";
import type { LeadRepositoryPort } from "@/domain/crm/ports/leadRepositoryPort";
import type { RealtimeChannelPort } from "@/domain/realtime/ports/realtimeChannelPort";
import { getFirebaseFirestore } from "./firebase/firebaseClient";
import {
  LEAD_COLLECTION_NAME,
  FirestoreLeadRepository,
} from "./firebase/firestoreLeadRepository";
import { FirestoreRealtimeChannel } from "./firebase/firestoreRealtimeChannel";
import { mapLeadDocumentData } from "./firebase/mappers/leadDocumentMapper";

/** Everything the React realtime layer may consume, typed by ports. */
export interface RealtimeContainer {
  /** Generic transport port; subscribe to any collection mapped to a domain type. */
  readonly realtimeChannel: RealtimeChannelPort<Lead>;
  /** CRM lead port used by the application service. */
  readonly leadRepository: LeadRepositoryPort;
  /** Use-case service the React hooks talk to. */
  readonly leadRealtimeService: LeadRealtimeService;
}

/**
 * Singleton wiring. Every React hook shares ONE container, but the concrete
 * Firestore adapters are only materialized on first real use (see
 * getLeadRepository) so module evaluation never touches env or Firestore.
 */
export const REALTIME_CONTAINER: RealtimeContainer =
  createLazyRealtimeContainer();

function createLazyRealtimeContainer(): RealtimeContainer {
  let builtRealtimeChannel: RealtimeChannelPort<Lead> | null = null;
  let builtLeadRepository: LeadRepositoryPort | null = null;

  /** Materializes the Firestore adapters exactly once, on the first real use. */
  function getLeadRepository(): LeadRepositoryPort {
    if (!builtLeadRepository) {
      const firestore = getFirebaseFirestore();
      const realtimeChannel = new FirestoreRealtimeChannel<Lead>(
        firestore,
        LEAD_COLLECTION_NAME,
        mapLeadDocumentData
      );
      builtRealtimeChannel = realtimeChannel;
      builtLeadRepository = new FirestoreLeadRepository(
        realtimeChannel,
        firestore
      );
    }
    return builtLeadRepository;
  }

  function getRealtimeChannel(): RealtimeChannelPort<Lead> {
    if (!builtRealtimeChannel) {
      getLeadRepository();
    }
    // Guaranteed non-null after getLeadRepository() ran at least once.
    return builtRealtimeChannel as RealtimeChannelPort<Lead>;
  }

  // The application service is pure TypeScript, so constructing it is
  // SSR-safe; only its repository dependency delegates to Firestore below.
  const leadRealtimeService = new LeadRealtimeService({
    streamLeadsByProject(request, handlers) {
      return getLeadRepository().streamLeadsByProject(request, handlers);
    },
    getLeadById(leadId) {
      return getLeadRepository().getLeadById(leadId);
    },
    saveLead(lead) {
      return getLeadRepository().saveLead(lead);
    },
  });

  return {
    realtimeChannel: {
      subscribeToDocument(request, handlers) {
        return getRealtimeChannel().subscribeToDocument(request, handlers);
      },
      subscribeToQuery(request, handlers) {
        return getRealtimeChannel().subscribeToQuery(request, handlers);
      },
    },
    leadRepository: {
      streamLeadsByProject(request, handlers) {
        return getLeadRepository().streamLeadsByProject(request, handlers);
      },
      getLeadById(leadId) {
        return getLeadRepository().getLeadById(leadId);
      },
      saveLead(lead) {
        return getLeadRepository().saveLead(lead);
      },
    },
    leadRealtimeService,
  };
}
