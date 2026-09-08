/**
 * Short-name wiring tests (backend GET /api/projects adds an additive
 * `short_name`, e.g. "The Soleil"). Pinned here:
 *  1. projectShortName prefers the compact contract field;
 *  2. payloads without short_name fall back to the full display name so the
 *     header renders exactly as before the backend change;
 *  3. the static seed catalogue carries the same short names the backend ships.
 */
import { describe, expect, it } from "vitest";
import {
  FALLBACK_ACTIVE_PROJECTS,
  projectDisplayName,
  projectShortName,
} from "@/features/chat/activeProjects";

describe("projectShortName", () => {
  it("prefers the backend short_name contract field", () => {
    expect(
      projectShortName({ project_key: "soleil", name: "The Soleil Đà Nẵng", short_name: "The Soleil" })
    ).toBe("The Soleil");
  });

  it("falls back to the full display name when short_name is absent", () => {
    expect(projectShortName({ project_key: "soleil", name: "The Soleil Đà Nẵng" })).toBe(
      "The Soleil Đà Nẵng"
    );
  });

  it("prefers the bounded display_name while retaining the legal name", () => {
    expect(projectDisplayName({
      project_key: "soleil",
      name: "The Soleil Đà Nẵng with a long commercial suffix",
      display_name: "The Soleil",
      ten_phap_ly: "Tổ hợp Ánh Dương - Soleil",
    })).toBe("The Soleil");
  });

  it("still honours the legacy alias chain through projectDisplayName", () => {
    // Legacy payloads carry the commercial name in ten_thuong_mai only.
    const legacy = {
      project_key: "x",
      name: undefined,
      ten_thuong_mai: "Legacy name",
    } as unknown as Parameters<typeof projectShortName>[0];
    expect(projectShortName(legacy)).toBe("Legacy name");
  });

  it("the static seed catalogue carries short names matching the backend values", () => {
    expect(FALLBACK_ACTIVE_PROJECTS.find((p) => p.project_key === "soleil")?.short_name).toBe(
      "The Soleil"
    );
    expect(FALLBACK_ACTIVE_PROJECTS.find((p) => p.project_key === "camellia")?.short_name).toBe(
      "The Camellia"
    );
  });
});
