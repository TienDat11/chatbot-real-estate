/**
 * FirestoreRealtimeChannel — Firestore adapter implementing RealtimeChannelPort.
 *
 * Binds one Firestore collection to one domain entity type. Every Firestore
 * snapshot is decoded through the injected mapper BEFORE handlers run, so
 * subscribers always receive domain entities. Connection state is mapped onto
 * the transport-agnostic RealtimeConnectionState: the first snapshot proves
 * the listener is active; listener errors become RealtimeChannelError.
 */
import {
  collection,
  doc,
  limit,
  onSnapshot,
  orderBy,
  query,
  where,
  type DocumentData,
  type Firestore,
} from "firebase/firestore";
import type {
  RealtimeChannelPort,
  RealtimeDocumentSubscriptionRequest,
  RealtimeEntitySubscriptionHandlers,
  RealtimeQuerySubscriptionRequest,
} from "@/domain/realtime/ports/realtimeChannelPort";
import type {
  RealtimeConnectionState,
  RealtimeSubscriptionHandle,
} from "@/domain/realtime/realtimeSubscription";
import { RealtimeChannelError } from "@/domain/realtime/realtimeSubscription";

export class FirestoreRealtimeChannel<TEntity>
  implements RealtimeChannelPort<TEntity>
{
  constructor(
    private readonly firestore: Firestore,
    private readonly collectionName: string,
    private readonly decodeDocumentData: (
      documentData: DocumentData,
      documentId: string
    ) => TEntity
  ) {}

  subscribeToDocument(
    request: RealtimeDocumentSubscriptionRequest,
    handlers: RealtimeEntitySubscriptionHandlers<TEntity>
  ): RealtimeSubscriptionHandle {
    notifyConnectionState(handlers, "connecting");

    const unsubscribe = onSnapshot(
      doc(this.firestore, this.collectionName, request.documentId),
      (snapshot) => {
        notifyConnectionState(handlers, "active");
        const entities = snapshot.exists()
          ? [this.decodeDocumentData(snapshot.data(), snapshot.id)]
          : [];
        handlers.onDocumentsChanged(entities);
      },
      (error) => notifyError(handlers, error)
    );

    return { unsubscribe };
  }

  subscribeToQuery(
    request: RealtimeQuerySubscriptionRequest,
    handlers: RealtimeEntitySubscriptionHandlers<TEntity>
  ): RealtimeSubscriptionHandle {
    notifyConnectionState(handlers, "connecting");

    const queryConstraints = [
      ...(request.filters ?? []).map((filter) =>
        where(filter.fieldPath, filter.operator, filter.value)
      ),
      ...(request.orderBy
        ? [orderBy(request.orderBy.fieldPath, request.orderBy.direction)]
        : []),
      ...(request.limit ? [limit(request.limit)] : []),
    ];

    const unsubscribe = onSnapshot(
      query(
        collection(this.firestore, this.collectionName),
        ...queryConstraints
      ),
      (snapshot) => {
        notifyConnectionState(handlers, "active");
        handlers.onDocumentsChanged(
          snapshot.docs.map((documentSnapshot) =>
            this.decodeDocumentData(documentSnapshot.data(), documentSnapshot.id)
          )
        );
      },
      (error) => notifyError(handlers, error)
    );

    return { unsubscribe };
  }
}

function notifyConnectionState<TEntity>(
  handlers: RealtimeEntitySubscriptionHandlers<TEntity>,
  state: RealtimeConnectionState
): void {
  handlers.onConnectionStateChanged?.(state);
}

function notifyError<TEntity>(
  handlers: RealtimeEntitySubscriptionHandlers<TEntity>,
  error: unknown
): void {
  const message =
    error instanceof Error
      ? error.message
      : "Realtime channel failed with an unknown error.";
  handlers.onConnectionStateChanged?.("error");
  handlers.onError?.(new RealtimeChannelError(message, { cause: error }));
}
