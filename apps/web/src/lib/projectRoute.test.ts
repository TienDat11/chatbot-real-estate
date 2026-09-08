/**
 * Pure redirect-decision tests for the project URL scheme.
 *
 * Pinned here (routing feature contract):
 *  1. bare "/" redirects to /project/<FIRST catalogue entry> — the default is
 *     whatever the backend lists first, never a client-side constant;
 *  2. legacy ?project_key=<known> links convert to the path form;
 *  3. a legacy key that is no longer active falls back to the first entry
 *     with an explicit "legacy_unknown" verdict so the caller can notify;
 *  4. a routed /project/<key> segment is accepted only when active; unknown
 *     keys yield a redirect back to the default.
 */
import { describe, expect, it } from "vitest";
import {
  decideBareRootRedirect,
  decideRoutedProject,
  projectPath,
} from "@/lib/projectRoute";
import type { ProjectSummary } from "@/lib/projectCatalog";

const CATALOG: ProjectSummary[] = [
  { project_key: "camellia", display_name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true },
  { project_key: "soleil", display_name: "The Soleil Đà Nẵng" },
];

describe("decideBareRootRedirect", () => {
  it("sends bare \"/\" to the FIRST backend entry, not a hardcoded default", () => {
    const decision = decideBareRootRedirect(CATALOG, "");
    expect(decision).toEqual({ kind: "redirect", path: "/project/camellia" });
    // Reversing the list moves the default — proof there is no client-side
    // constant hiding in the decision.
    const flipped = decideBareRootRedirect([...CATALOG].reverse(), "");
    expect(flipped).toEqual({ kind: "redirect", path: "/project/soleil" });
  });

  it("converts a known legacy ?project_key link to the path form", () => {
    const decision = decideBareRootRedirect(CATALOG, "?project_key=soleil");
    expect(decision).toEqual({ kind: "redirect", path: "/project/soleil" });
  });

  it("flags an unknown legacy key and falls back to the default", () => {
    const decision = decideBareRootRedirect(CATALOG, "?project_key=ghost");
    expect(decision).toEqual({ kind: "legacy_unknown", fallbackPath: "/project/camellia" });
  });

  it("treats an empty legacy param like no param at all", () => {
    const decision = decideBareRootRedirect(CATALOG, new URLSearchParams("project_key="));
    expect(decision).toEqual({ kind: "redirect", path: "/project/camellia" });
  });
});

describe("decideRoutedProject", () => {
  it("accepts an active project key", () => {
    expect(decideRoutedProject(CATALOG, "soleil")).toEqual({ kind: "accept" });
  });

  it("sends an unknown segment back to the default project", () => {
    expect(decideRoutedProject(CATALOG, "vinhome")).toEqual({
      kind: "unknown",
      fallbackPath: "/project/camellia",
    });
  });
});

describe("projectPath", () => {
  it("encodes unsafe characters in the key", () => {
    expect(projectPath("camellia")).toBe("/project/camellia");
    expect(projectPath("a b/c")).toBe("/project/a%20b%2Fc");
  });
});
