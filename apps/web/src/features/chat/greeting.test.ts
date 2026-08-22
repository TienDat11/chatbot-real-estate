import { describe, expect, it } from "vitest";
import { greetingForProject, SOLEIL_GREETING_TEXT } from "@/features/chat/greeting";
import { GREETING_STATIC_TEXT, GREETING_IMAGES, GREETING_VIDEOS } from "@/lib/greetingContent";

// Wave-1 UX: the greeting must follow the chosen project. The FE greeting is
// static content (instant render, no network), so per-project consistency is a
// pure mapping from project_key to the greeting bundle — a Soleil first-open
// must never greet as Camellia.

describe("greetingForProject", () => {
  it("greets a Soleil visitor with grounded Soleil copy and no media", () => {
    const bundle = greetingForProject("soleil");
    expect(bundle.text).toBe(SOLEIL_GREETING_TEXT);
    expect(bundle.text).toContain("The Soleil Đà Nẵng");
    expect(bundle.text).not.toContain("The Camellia");
    expect(bundle.images).toHaveLength(0);
    expect(bundle.videos).toHaveLength(0);
  });

  it("greets Camellia with the rich media bundle (text + gallery + films)", () => {
    const bundle = greetingForProject("camellia");
    expect(bundle.text).toBe(GREETING_STATIC_TEXT);
    expect(bundle.images).toEqual(GREETING_IMAGES);
    expect(bundle.videos).toEqual(GREETING_VIDEOS);
  });

  it("defaults unknown and empty keys to the Camellia bundle (registry default)", () => {
    expect(greetingForProject(null).text).toBe(GREETING_STATIC_TEXT);
    expect(greetingForProject(undefined).text).toBe(GREETING_STATIC_TEXT);
    expect(greetingForProject("").text).toBe(GREETING_STATIC_TEXT);
  });
});
