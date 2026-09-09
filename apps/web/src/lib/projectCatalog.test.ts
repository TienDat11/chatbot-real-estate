/**
 * Adapter contract tests for GET /api/projects (URL-routing feature).
 *
 * Pinned here:
 *  1. the current contract shape `{"projects":[{"project_key","display_name"}]}`
 *     maps cleanly;
 *  2. every legacy alias the backend has shipped (name/ten_thuong_mai,
 *     vi_tri) still maps so shape drift stays a one-file fix;
 *  3. unusable payloads (missing/empty list, junk rows only) throw
 *     ProjectCatalogUnavailableError instead of returning a routable empty
 *     list;
 *  4. fetchProjectCatalog retries transient failures and rejects only after
 *     every attempt.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchProjectCatalog,
  parseProjectCatalog,
  ProjectCatalogUnavailableError,
} from "@/lib/projectCatalog";

describe("parseProjectCatalog", () => {
  it("maps the current contract shape (project_key + display_name)", () => {
    const catalog = parseProjectCatalog({
      projects: [
        { project_key: "camellia", display_name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true },
        { project_key: "soleil", display_name: "The Soleil Đà Nẵng" },
      ],
    });
    expect(catalog).toEqual([
      { project_key: "camellia", display_name: "The Camellia Sơn Trà - Đà Nẵng", is_hot: true },
      { project_key: "soleil", display_name: "The Soleil Đà Nẵng" },
    ]);
  });

  it("accepts legacy aliases (name/ten_thuong_mai, vi_tri) and preserves order", () => {
    const catalog = parseProjectCatalog({
      projects: [
        { project_key: "soleil", name: "Soleil legacy", location: "ĐN" },
        { project_key: "camellia", ten_thuong_mai: "Camellia legacy", vi_tri: "ĐN", lat: 16.1, lng: 108.25 },
      ],
    });
    expect(catalog.map((p) => p.project_key)).toEqual(["soleil", "camellia"]);
    expect(catalog[0].display_name).toBe("Soleil legacy");
    expect(catalog[0].location).toBe("ĐN");
    // display_name wins over the legacy name alias when both exist.
    const both = parseProjectCatalog({
      projects: [{ project_key: "x", display_name: "new", name: "old" }],
    });
    expect(both[0].display_name).toBe("new");
  });

  it("drops junk rows but keeps usable ones around them", () => {
    const catalog = parseProjectCatalog({
      projects: [null, "nope", { project_key: "ok", display_name: "OK" }, { project_key: "" }, {}],
    });
    expect(catalog).toHaveLength(1);
    expect(catalog[0].project_key).toBe("ok");
  });

  it("throws when the payload carries no usable project", () => {
    expect(() => parseProjectCatalog({ projects: [] })).toThrow(ProjectCatalogUnavailableError);
    expect(() => parseProjectCatalog({})).toThrow(ProjectCatalogUnavailableError);
    expect(() => parseProjectCatalog(null)).toThrow(ProjectCatalogUnavailableError);
    expect(() => parseProjectCatalog({ projects: ["junk"] })).toThrow(ProjectCatalogUnavailableError);
  });

  it("carries the additive short_name field and keeps display_name untouched", () => {
    const catalog = parseProjectCatalog({
      projects: [{ project_key: "soleil", display_name: "The Soleil Đà Nẵng", short_name: "The Soleil" }],
    });
    expect(catalog[0].display_name).toBe("The Soleil Đà Nẵng");
    expect(catalog[0].short_name).toBe("The Soleil");
  });

  it("leaves short_name undefined for legacy payloads so consumers fall back to display_name", () => {
    const catalog = parseProjectCatalog({
      projects: [{ project_key: "soleil", display_name: "The Soleil Đà Nẵng" }],
    });
    expect(catalog[0].short_name).toBeUndefined();
    // Documented consumer fallback: short_name ?? display_name.
    expect(catalog[0].short_name ?? catalog[0].display_name).toBe("The Soleil Đà Nẵng");
  });
});

describe("fetchProjectCatalog", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("parses the endpoint body on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ projects: [{ project_key: "camellia", display_name: "Camellia" }] }), {
            status: 200,
          })
        )
      )
    );
    const catalog = await fetchProjectCatalog({ retryDelayMs: 0 });
    expect(catalog).toHaveLength(1);
    expect(catalog[0].display_name).toBe("Camellia");
  });

  it("retries failed attempts then rejects with the typed error", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response("{}", { status: 500 })));
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchProjectCatalog({ retries: 2, retryDelayMs: 0 })).rejects.toThrow(
      ProjectCatalogUnavailableError
    );
    // 1 initial attempt + 2 retries.
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("treats malformed JSON bodies as unavailable, not as an empty catalogue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ unrelated: true }), { status: 200 })))
    );
    await expect(fetchProjectCatalog({ retries: 0, retryDelayMs: 0 })).rejects.toThrow(
      ProjectCatalogUnavailableError
    );
  });
});
