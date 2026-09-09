import { describe, expect, it } from "vitest";
import {
  applySessionParam,
  canonicalProjectUrl,
  projectChatUrl,
  resolveSessionParam,
  salesChatProjectUrl,
} from "@/lib/canonicalProjectUrl";

describe("canonicalProjectUrl", () => {
  it("writes the canonical sessionId param, encoded", () => {
    expect(canonicalProjectUrl("soleil", "session/one")).toBe(
      "/project/soleil?sessionId=session%2Fone",
    );
  });

  it.each([undefined, null, ""])("omits falsy sessions", (session) => {
    expect(canonicalProjectUrl("camellia", session)).toBe("/project/camellia");
  });

  it("encodes the project key (deep-link safe)", () => {
    expect(canonicalProjectUrl("a b/c", "id")).toBe("/project/a%20b%2Fc?sessionId=id");
  });

  it("never writes the legacy ?session= spelling", () => {
    const url = canonicalProjectUrl("camellia", "s1");
    expect(url).not.toContain("?session=");
    expect(url).toContain("sessionId=s1");
  });
});

describe("resolveSessionParam (route-page reader)", () => {
  it("prefers canonical sessionId over legacy session", () => {
    expect(resolveSessionParam({ sessionId: "new", session: "old" })).toBe("new");
  });

  it("falls back to a legacy session deep link", () => {
    expect(resolveSessionParam({ session: "legacy-id" })).toBe("legacy-id");
  });

  it("takes the first value when Next hands an array per key", () => {
    expect(resolveSessionParam({ sessionId: ["first", "second"] })).toBe("first");
    expect(resolveSessionParam({ session: ["only"] })).toBe("only");
  });

  it("treats empty strings as absent without shadowing the legacy fallback", () => {
    expect(resolveSessionParam({ sessionId: "", session: "kept" })).toBe("kept");
    expect(resolveSessionParam({})).toBeUndefined();
    expect(resolveSessionParam({ sessionId: "", session: "" })).toBeUndefined();
  });

  it("round-trips through canonicalProjectUrl", () => {
    const url = canonicalProjectUrl("camellia", "abc");
    const search = Object.fromEntries(new URLSearchParams(url.split("?")[1] ?? ""));
    // Mirror the route page: the query object after Next's await.
    expect(resolveSessionParam(search)).toBe("abc");
  });
});

describe("applySessionParam (in-place URL rewrite)", () => {
  it("sets the canonical key and drops the legacy one so both never coexist", () => {
    const params = new URLSearchParams("session=stale&mode=list");
    applySessionParam(params, "fresh");
    expect(params.get("sessionId")).toBe("fresh");
    expect(params.has("session")).toBe(false);
  });

  it("preserves unrelated project/history params and never duplicates keys", () => {
    const params = new URLSearchParams("mode=list&session=old");
    applySessionParam(params, "id1");
    applySessionParam(params, "id2");
    const seen = params.getAll("sessionId");
    expect(seen).toEqual(["id2"]);
    expect(params.toString()).toBe("mode=list&sessionId=id2");
  });
});

describe("sales chat surface URLs", () => {
  it("salesChatProjectUrl builds the canonical sales route with an encoded key/session", () => {
    expect(salesChatProjectUrl("a b", "s/1")).toBe(
      "/sales/chat/project/a%20b?sessionId=s%2F1",
    );
    expect(salesChatProjectUrl("soleil", null)).toBe("/sales/chat/project/soleil");
  });

  it("projectChatUrl keeps each surface on its own canonical route", () => {
    expect(projectChatUrl("sales", "soleil", "s1")).toBe(
      "/sales/chat/project/soleil?sessionId=s1",
    );
    expect(projectChatUrl("customer", "soleil", "s1")).toBe("/project/soleil?sessionId=s1");
    expect(projectChatUrl("sales", "soleil", "")).toBe("/sales/chat/project/soleil");
  });
});
