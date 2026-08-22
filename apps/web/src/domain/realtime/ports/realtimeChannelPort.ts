/**
 * RealtimeChannelPort — the transport-agnostic "subscribe" contract.
 *
 * This is an OUTBOUND port in the hexagonal sense: the domain defines the
 * capability ("subscribe to a document, subscribe to a filtered query") and
 * infrastructure adapters implement it. The React layer and application
 * services only ever see this interface, so swapping Firestore for
 * WebSocket/Socket.IO means adding one adapter and rewiring the composition
 * root — domain/ and application/ never change.
 *
 * Handlers receive DOMAIN entities, never raw transport payloads. The concrete
 * adapter owns the mapping (e.g. Firestore DocumentData -> Lead via the
 * leadDocumentMapper).
 */
import type {
  RealtimeChannelError,
  RealtimeConnectionState,
  RealtimeSubscriptionHandle,
} from "@/domain/realtime/realtimeSubscription";

/** Comparison operators a query filter may use, kept intentionally small. */
export type RealtimeQueryOperator =
  | "=="
  | "!="
  | ">"
  | ">="
  | "<"
  | "<="
  | "array-contains"
  | "in"
  | "not-in";

export interface RealtimeQueryFilter {
  fieldPath: string;
  operator: RealtimeQueryOperator;
  value: unknown;
}

export interface RealtimeQueryOrderBy {
  fieldPath: string;
  direction: "asc" | "desc";
}

/** Request for a single-document subscription. */
export interface RealtimeDocumentSubscriptionRequest {
  documentId: string;
}

/**
 * Request for a collection/query subscription.
 *
 * `collectionName` is the transport resource name: a Firestore collection id
 * today, a Socket.IO room name after a WebSocket swap.
 */
export interface RealtimeQuerySubscriptionRequest {
  collectionName: string;
  filters?: RealtimeQueryFilter[];
  orderBy?: RealtimeQueryOrderBy;
  limit?: number;
}

/** Callbacks a subscriber supplies; every callback receives DOMAIN entities. */
export interface RealtimeEntitySubscriptionHandlers<TEntity> {
  /** Full current result set on every change (added/removed/modified). */
  onDocumentsChanged(entities: TEntity[]): void;
  onError?(error: RealtimeChannelError): void;
  onConnectionStateChanged?(state: RealtimeConnectionState): void;
}

/** Contract implemented by every realtime transport adapter. */
export interface RealtimeChannelPort<TEntity> {
  subscribeToDocument(
    request: RealtimeDocumentSubscriptionRequest,
    handlers: RealtimeEntitySubscriptionHandlers<TEntity>
  ): RealtimeSubscriptionHandle;

  subscribeToQuery(
    request: RealtimeQuerySubscriptionRequest,
    handlers: RealtimeEntitySubscriptionHandlers<TEntity>
  ): RealtimeSubscriptionHandle;
}
