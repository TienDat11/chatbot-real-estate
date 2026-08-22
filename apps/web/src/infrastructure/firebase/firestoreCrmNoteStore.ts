/**
 * FirestoreCrmNoteStore — Firestore adapter implementing CrmNoteStorePort.
 *
 * FIRESTORE-NATIVE exception (story 9.3): notes live ONLY in Firestore
 * (`notes/{leadId}`), written by the staff client, never mirrored back to the
 * backend. The security rules allow sales/admin writes for exactly this
 * collection. Every other CRM write (status, consent) still goes through the
 * backend REST API because the backend owns that state.
 */
import {
  doc,
  getDoc,
  serverTimestamp,
  setDoc,
  Timestamp,
  type Firestore,
} from "firebase/firestore";
import type {
  CrmNoteSnapshot,
  CrmNoteStorePort,
  SaveCrmNoteRequest,
} from "@/domain/crm/ports/crmNoteStorePort";

/** Firestore collection holding staff-authored CRM notes. */
export const CRM_NOTE_COLLECTION_NAME = "notes";

export class FirestoreCrmNoteStore implements CrmNoteStorePort {
  constructor(private readonly firestore: Firestore) {}

  async readLeadNote(leadId: string): Promise<CrmNoteSnapshot | null> {
    const documentSnapshot = await getDoc(
      doc(this.firestore, CRM_NOTE_COLLECTION_NAME, leadId)
    );
    if (!documentSnapshot.exists()) {
      return null;
    }
    const data = documentSnapshot.data();
    return {
      content: typeof data.content === "string" ? data.content : "",
      updatedByUid: typeof data.updated_by_uid === "string" ? data.updated_by_uid : null,
      updatedAt: toIso8601StringOrNull(data.updated_at),
    };
  }

  async saveLeadNote(request: SaveCrmNoteRequest): Promise<void> {
    await setDoc(
      doc(this.firestore, CRM_NOTE_COLLECTION_NAME, request.leadId),
      {
        content: request.content,
        updated_by_uid: request.authorUid,
        // serverTimestamp keeps ordering authoritative even when a staff
        // device clock drifts.
        updated_at: serverTimestamp(),
      },
      { merge: true }
    );
  }
}

/** Firestore Timestamp (or stored ISO string) -> ISO-8601 string, null-safe. */
function toIso8601StringOrNull(value: unknown): string | null {
  if (value instanceof Timestamp) {
    return value.toDate().toISOString();
  }
  if (typeof value === "string") {
    return value;
  }
  return null;
}
