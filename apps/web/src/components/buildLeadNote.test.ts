import { describe, expect, it } from "vitest";
import type { FactEvidence } from "@rag-ragre/contracts";
import { buildLeadNote } from "@/components/ChatPage";

// Story 5.7 note prefill: the broker note is built from the most recent
// assistant answer that carries facts, capped at 200 chars. User messages,
// fact-less answers and blank subjects must never produce a note.
function fact(subject: string): FactEvidence {
  return { fe_id: "fe-1", subject, fields: {} };
}

function assistant(facts: FactEvidence[]): Parameters<typeof buildLeadNote>[0][number] {
  return { id: "a1", role: "assistant", content: "x", facts };
}

function user(): Parameters<typeof buildLeadNote>[0][number] {
  return { id: "u1", role: "user", content: "x" };
}

describe("buildLeadNote", () => {
  it("returns undefined when there are no messages", () => {
    expect(buildLeadNote([])).toBeUndefined();
  });

  it("returns undefined when no assistant message has facts", () => {
    expect(buildLeadNote([user(), { id: "a1", role: "assistant", content: "hi" }])).toBeUndefined();
  });

  it("joins the subjects of the latest factual answer", () => {
    const note = buildLeadNote([
      user(),
      assistant([fact("2PN view nội khu")]),
      user(),
      assistant([fact("Giá 4 tỷ"), fact("Pháp lý sổ hồng")]),
    ]);
    expect(note).toBe("Quan tâm: Giá 4 tỷ, Pháp lý sổ hồng");
  });

  it("ignores a newer fact-less answer and uses the previous factual one", () => {
    const note = buildLeadNote([assistant([fact("2PN")]), { id: "a2", role: "assistant", content: "ok" }]);
    expect(note).toBe("Quan tâm: 2PN");
  });

  it("skips blank subjects within a factual answer", () => {
    const note = buildLeadNote([assistant([fact("   "), fact("Căn góc")])]);
    expect(note).toBe("Quan tâm: Căn góc");
  });

  it("returns undefined when all subjects are blank", () => {
    expect(buildLeadNote([assistant([fact(" "), fact("\t")])])).toBeUndefined();
  });

  it("caps the note at 200 characters", () => {
    const long = Array.from({ length: 30 }, (_, i) => `Sự kiện thứ ${i + 1}`).join(", ");
    const note = buildLeadNote([assistant([fact(long)])]);
    expect(note?.length).toBeLessThanOrEqual(200);
    expect(note?.length).toBe(200);
  });
});
