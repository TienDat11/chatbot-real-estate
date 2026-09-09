import { describe, expect, it } from "vitest";
import { sameOriginNotificationPath } from "@/infrastructure/firebase/firebaseNotificationUtils";

describe("sameOriginNotificationPath", () => {
  it("accepts same-origin absolute paths", () => {
    expect(sameOriginNotificationPath("/sales?lead=12#details")).toBe("/sales?lead=12#details");
  });
  it("rejects protocol-relative paths", () => {
    expect(sameOriginNotificationPath("//evil.example/path")).toBe("/");
  });
  it("rejects absolute URLs", () => {
    expect(sameOriginNotificationPath("https://evil.example/path")).toBe("/");
    expect(sameOriginNotificationPath("http://evil.example/path")).toBe("/");
  });
});
