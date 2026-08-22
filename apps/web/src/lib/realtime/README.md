# lib/realtime — React binding for the realtime/CRM layer

This folder is the ONLY part of the React layer that touches realtime. It
consumes the composition root (`src/infrastructure/realtimeContainer.ts`)
through the domain port types — never raw Firestore objects.

```
src/
  domain/            pure TS: Lead entity, RealtimeChannelPort, LeadRepositoryPort
  application/       use-case service (LeadRealtimeService), port-only deps
  infrastructure/    transport adapters + REALTIME_CONTAINER (the only layer importing firebase)
  lib/realtime/      React context + hooks + this README
```

Dependency direction is inward: `lib/realtime` -> `infrastructure/realtimeContainer`
-> adapters -> ports -> domain. Swapping transport touches exactly ONE layer.

## How to swap Firestore -> Socket.IO (the whole story)

Firestore is an ADAPTER choice, not an architectural one. To move to
WebSocket/Socket.IO, edit only `src/infrastructure/`:

1. **Add `src/infrastructure/websocket/websocketRealtimeChannel.ts`** that
   implements `RealtimeChannelPort<TEntity>` (same subscribeToDocument /
   subscribeToQuery contract). A Socket.IO implementation is roughly:
   - `subscribeToQuery({ collectionName, filters, orderBy, limit }, handlers)`
     maps to a room join: `socket.emit("join", collectionName, filters)` and a
     listener `socket.on("leads:updated", (payload) => handlers.onDocumentsChanged(payload.entities))`.
     The server owns filtering/sorting/limiting; the adapter just relays domain
     entities (it can reuse the existing `leadDocumentMapper` on the server
     payload when the payload is snake_case).
   - `subscribeToDocument({ documentId })` maps to `socket.emit("join-doc", documentId)`.
   - Connection state: `socket.on("connect")` -> "active", `socket.on("disconnect")`
     -> "connecting", `socket.on("connect_error")` -> "error" (wrap into
     `RealtimeChannelError`).
   - If the WebSocket transport is read-only, the companion repository's
     `saveLead` throws `TransportCapabilityNotSupportedError`.

2. **Add `src/infrastructure/websocket/websocketLeadRepository.ts`** that
   implements `LeadRepositoryPort` and delegates `streamLeadsByProject` to the
   channel above (mirror `firestoreLeadRepository.ts`).

3. **Edit `src/infrastructure/realtimeContainer.ts`** — the composition root —
   replacing the two Firestore adapter lines with the WebSocket adapters. That
   is the ENTIRE diff:

   ```ts
   // BEFORE (Firestore)
   const firestore = getFirebaseFirestore();
   const realtimeChannel = new FirestoreRealtimeChannel<Lead>(firestore, LEAD_COLLECTION_NAME, mapLeadDocumentData);
   const leadRepository = new FirestoreLeadRepository(realtimeChannel, firestore);

   // AFTER (Socket.IO)
   const socket = connectWebSocketClient(); // your socket factory
   const realtimeChannel = new WebSocketRealtimeChannel<Lead>(socket, mapLeadDocumentData);
   const leadRepository = new WebSocketLeadRepository(realtimeChannel);
   ```

**Untouched by the swap:** `src/domain/`, `src/application/`,
`src/lib/realtime/` (this folder), and the existing unit test
`src/application/crm/leadRealtimeService.test.ts` — it drives the service with
an in-memory repository and proves no layer above infrastructure cares about
the transport.

### Mapping notes (Firestore field -> Socket.IO concept)

| Firestore                          | Socket.IO                          |
| ---------------------------------- | ---------------------------------- |
| collection `leads`                 | room name `leads`                  |
| `where(project_key == X)`          | server-side filter in the room     |
| `orderBy(created_at desc)` + limit | server-side sort/limit in the room |
| `onSnapshot`                       | `socket.on("leads:updated")`       |
| listener error callback            | `socket.on("connect_error")`       |
| listener detach (unsubscribe)      | `socket.off(...)` + leave room     |

## Mounting (wave 2 — do NOT mount yet)

`RealtimeProvider` is intentionally NOT in `app/layout.tsx`. The wave-2 story
(Firestore lead mirror + CRM UI) owns mounting:

```tsx
// Wherever the CRM board renders:
<RealtimeProvider>
  <LeadBoard />
</RealtimeProvider>
```

Until then, consumers of `useLeadRealtimeStream` must be wrapped in a
`RealtimeProvider` by their own page/section.
