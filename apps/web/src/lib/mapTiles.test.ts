/**
 * Tile provider config + failure-tracker unit tests (node env).
 *
 * Pinned here:
 *  1. the built-in default is a Vietnam-reachable CDN, never plain
 *     tile.openstreetmap.org (which blank-canvas the map on VN networks);
 *  2. NEXT_PUBLIC_MAP_TILE_URL override wins and trims whitespace;
 *  3. {s}/{r} placeholders are resolved before handing URLs to maplibre
 *     (it cannot substitute Leaflet-style tokens itself);
 *  4. attribution credits the provider actually serving the tiles;
 *  5. the failure tracker latches exactly at the threshold so repeated tile
 *     errors flip the UI fallback once.
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_MAP_TILE_URL,
  MAP_TILE_URL,
  TILE_FAILURE_THRESHOLD,
  attributionForTileUrl,
  createTileFailureTracker,
  expandTileUrls,
  isTileFetchError,
  resolveMapTileUrl,
} from "@/lib/mapTiles";

describe("tile URL config", () => {
  it("defaults to the CARTO CDN, never plain tile.openstreetmap.org", () => {
    expect(DEFAULT_MAP_TILE_URL).toContain("basemaps.cartocdn.com");
    expect(DEFAULT_MAP_TILE_URL).not.toMatch(/^https:\/\/tile\.openstreetmap\.org/);
    expect(MAP_TILE_URL).toBe(DEFAULT_MAP_TILE_URL);
  });

  it("honours the env override over the default", () => {
    expect(resolveMapTileUrl(undefined)).toBe(DEFAULT_MAP_TILE_URL);
    expect(resolveMapTileUrl("   ")).toBe(DEFAULT_MAP_TILE_URL);
    expect(resolveMapTileUrl("  https://tiles.example.vn/{z}/{x}/{y}.png ")).toBe(
      "https://tiles.example.vn/{z}/{x}/{y}.png"
    );
  });

  it("expands {s} into concrete subdomains and strips the retina token", () => {
    const urls = expandTileUrls(DEFAULT_MAP_TILE_URL);
    expect(urls).toHaveLength(4);
    const subdomains = ["a", "b", "c", "d"];
    urls.forEach((url, i) => {
      expect(url.startsWith(`https://${subdomains[i]}.basemaps.cartocdn.com/`)).toBe(true);
      expect(url).not.toContain("{s}");
      expect(url).not.toContain("{r}");
    });
  });

  it("keeps templates without {s} intact (minus the retina token)", () => {
    expect(expandTileUrls("https://t.example.vn/{z}/{x}/{y}{r}.png")).toEqual([
      "https://t.example.vn/{z}/{x}/{y}.png",
    ]);
  });

  it("credits the provider actually serving the tiles", () => {
    const carto = attributionForTileUrl(DEFAULT_MAP_TILE_URL);
    expect(carto).toContain("OpenStreetMap");
    expect(carto).toContain("CARTO");
    expect(attributionForTileUrl("https://tile.openstreetmap.org/{z}/{x}/{y}.png")).not.toContain(
      "CARTO"
    );
  });
});

describe("createTileFailureTracker", () => {
  it("latches exactly at the failure threshold", () => {
    const tracker = createTileFailureTracker();
    for (let i = 1; i < TILE_FAILURE_THRESHOLD; i += 1) {
      expect(tracker.record()).toBe(false);
    }
    expect(tracker.record()).toBe(true);
    expect(tracker.record()).toBe(true);
  });

  it("supports a custom threshold and resets cleanly", () => {
    const tracker = createTileFailureTracker(2);
    expect(tracker.record()).toBe(false);
    expect(tracker.record()).toBe(true);
    tracker.reset();
    expect(tracker.record()).toBe(false);
    expect(tracker.record()).toBe(true);
  });
});

describe("isTileFetchError", () => {
  it("accepts maplibre ErrorEvent wrappers and bare error shapes", () => {
    expect(isTileFetchError({ error: { status: 0, message: "AJAXError: Failed to fetch" } })).toBe(true);
    expect(isTileFetchError({ error: { status: 404, message: "Not Found" } })).toBe(true);
    expect(isTileFetchError({ message: "Failed to fetch" })).toBe(true);
  });

  it("rejects non-fetch errors and junk shapes", () => {
    expect(isTileFetchError({ error: { status: 200 } })).toBe(false);
    expect(isTileFetchError(new Error("layers[0]: style parse issue"))).toBe(false);
    expect(isTileFetchError(null)).toBe(false);
    expect(isTileFetchError("boom")).toBe(false);
  });
});
