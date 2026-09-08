import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const rulesPath = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../firestore.rules",
);
const rules = readFileSync(rulesPath, "utf8");

describe("Firestore note security contract", () => {
  it("uses the note id to enforce matching lead ownership", () => {
    expect(rules).toContain("function canAccessLead(leadId)");
    expect(rules).toContain("allow read: if canAccessLead(noteId);");
    expect(rules).toContain("allow create, update: if canAccessLead(noteId)");
    expect(rules).toContain("exists(/databases/$(database)/documents/leads/$(leadId))");
    expect(rules).toContain("assigned_sales_firebase_uid");
    expect(rules).toContain("== request.auth.uid");
  });

  it("requires an active sales profile while preserving verified admin access", () => {
    expect(rules).toContain("function isVerifiedAdmin()");
    expect(rules).toContain("request.auth.token.role == 'admin'");
    expect(rules).toContain("function isActiveSales()");
    expect(rules).toContain("callerSalesProfile().data.firebase_uid == request.auth.uid");
    expect(rules).toContain("callerSalesProfile().data.role == 'sales'");
    expect(rules).toContain("callerSalesProfile().data.is_active == true");
    expect(rules).toContain("return isVerifiedAdmin() || isActiveSales();");
    expect(rules).not.toContain("callerSalesProfile().data.role in ['sales', 'admin']");
  });

  it.each([
    ["active owner", "isActiveSales()", "assigned_sales_firebase_uid == request.auth.uid"],
    ["disabled owner", "callerSalesProfile().data.is_active == true", "isActiveSales()"],
    ["other sales", "assigned_sales_firebase_uid == request.auth.uid", "isActiveSales()"],
    ["unassigned lead", "exists(/databases/$(database)/documents/leads/$(leadId))", "canAccessLead(leadId)"],
    ["admin", "request.auth.token.role == 'admin'", "isVerifiedAdmin()"],
  ])("covers %s authorization path", (_caseName, required, gate) => {
    expect(rules).toContain(required);
    expect(rules).toContain(gate);
  });

  it("preserves author, content, delete, and lead write restrictions", () => {
    expect(rules).toContain("request.resource.data.updated_by_uid == request.auth.uid");
    expect(rules).toContain("request.resource.data.content is string");
    expect(rules).toContain("request.resource.data.content.size() <= 2000");
    expect(rules).toContain("allow delete: if false;");
    expect(rules).toContain("allow write: if false;");
    expect(rules).toContain("masked_phone");
  });

  it("whitelists the complete note schema and validates metadata types", () => {
    expect(rules).toContain(
      'request.resource.data.keys().hasOnly(\n          ["content", "updated_by_uid", "updated_at"]\n        )',
    );
    expect(rules).toContain("request.resource.data.updated_by_uid is string");
    expect(rules).toContain("request.resource.data.updated_at is timestamp");
    expect(rules).toContain("request.resource.data.content.size() <= 2000");
  });

  it("requires immutable author ownership on updates", () => {
    expect(rules).toContain(
      "request.resource.data.updated_by_uid\n                == resource.data.updated_by_uid",
    );
    expect(rules).toContain("exists(/databases/$(database)/documents/notes/$(noteId))");
  });

  it("keeps lead ownership tied to the immutable note id and rejects raw phone fields", () => {
    expect(rules).toContain("allow read: if canAccessLead(noteId);");
    expect(rules).toContain("canAccessLead(noteId)");
    expect(rules).not.toContain('hasOnly(["content", "updated_by_uid", "updated_at", "phone"])');
  });

  it("resolves note ownership through the lead's opaque Firestore document id (ADR-0004)", () => {
    // Lead documents live at opaque HMAC keys (ADR-0004); a note inherits that
    // identity. The rules must pin the note id to the lead's opaque document
    // id and dereference the lead document by the note id itself, so a wrong
    // key (a numeric lead_id matches no lead document) fails closed.
    expect(rules).toContain("The note document id IS the lead's");
    expect(rules).toContain("opaque Firestore document id (ADR-0004)");
    expect(rules).toContain("NEVER the numeric lead_id field");
    expect(rules).toContain("documents/leads/$(leadId)");
    // The stale numeric-id phrasing must stay out of the rules contract.
    expect(rules).not.toContain("the document id IS the lead id");
  });
});
