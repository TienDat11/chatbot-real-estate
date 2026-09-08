// @vitest-environment jsdom
/**
 * syncSessionUrl route whitelist: the canonical ?sessionId= write must land on
 * every shareable chat surface - the customer /project/* route, the routed
 * sales project route, and the bare /sales/chat and /sales/train workspaces -
 * while a non-chat route and an empty session id stay untouched.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { syncSessionUrl } from "@/components/ChatPage";

function navigate(path: string): void {
  window.history.replaceState(null, "", path);
}

function sessionIdParam(): string | null {
  return new URLSearchParams(window.location.search).get("sessionId");
}

describe("syncSessionUrl", () => {
  beforeEach(() => {
    navigate("/");
  });

  afterEach(() => {
    navigate("/");
  });

  it("writes ?sessionId= on /sales/train", () => {
    navigate("/sales/train");
    syncSessionUrl("train-sess-1");
    expect(sessionIdParam()).toBe("train-sess-1");
  });

  it("writes ?sessionId= on the bare /sales/chat workspace", () => {
    navigate("/sales/chat");
    syncSessionUrl("sales-sess-1");
    expect(sessionIdParam()).toBe("sales-sess-1");
  });

  it("writes ?sessionId= on the routed sales project surface", () => {
    navigate("/sales/chat/project/camellia");
    syncSessionUrl("routed-sess-1");
    expect(sessionIdParam()).toBe("routed-sess-1");
  });

  it("keeps writing ?sessionId= on the customer /project/* route", () => {
    navigate("/project/camellia");
    syncSessionUrl("cust-sess-1");
    expect(sessionIdParam()).toBe("cust-sess-1");
  });

  it("preserves unrelated query params while adding the session", () => {
    navigate("/sales/train?mode=list");
    syncSessionUrl("keep-sess");
    const params = new URLSearchParams(window.location.search);
    expect(params.get("sessionId")).toBe("keep-sess");
    expect(params.get("mode")).toBe("list");
  });

  it("does not write on a non-chat route", () => {
    navigate("/settings");
    syncSessionUrl("nope");
    expect(sessionIdParam()).toBeNull();
  });

  it("ignores an empty session id", () => {
    navigate("/sales/train");
    syncSessionUrl("");
    expect(sessionIdParam()).toBeNull();
  });
});
