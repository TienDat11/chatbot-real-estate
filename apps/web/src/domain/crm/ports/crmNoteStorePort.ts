/**
 * CrmNoteStorePort — staff-authored CRM note persistence (story 9.3).
 *
 * FIRESTORE-NATIVE EXCEPTION: unlike leads (mirrored backend->Firestore by the
 * REST mirror), CRM notes are written by the STAFF CLIENT directly to the
 * `notes` collection and are never mirrored back through the backend. The
 * backend therefore has no note concept at all; this port exists so the UI
 * still depends on an interface, and the Firestore adapter stays the only
 * place that knows the collection shape.
 */

/** Last persisted state of one staff note, as shown in the note editor. */
export interface CrmNoteSnapshot {
  /** Staff-typed note text; empty string means "cleared". */
  content: string;
  /** Firebase uid of the staff member who last saved the note, when known. */
  updatedByUid: string | null;
  /** ISO-8601 instant of the last save, when the store provides one. */
  updatedAt: string | null;
}

/** Write request for saving a note; the lead id keys the note document. */
export interface SaveCrmNoteRequest {
  /** Opaque lead id — also the note document id (`notes/{leadId}`). */
  leadId: string;
  content: string;
  /** Firebase uid of the signed-in staff member saving the note. */
  authorUid: string;
}

/** Capability contract for the FIRESTORE-NATIVE note store. */
export interface CrmNoteStorePort {
  /** Reads the current note of a lead, or null when none was saved yet. */
  readLeadNote(leadId: string): Promise<CrmNoteSnapshot | null>;

  /** Saves (overwrites) the note of a lead. */
  saveLeadNote(request: SaveCrmNoteRequest): Promise<void>;
}
