/**
 * Tile provider config + failure-tracker unit tests (node env).
 *
 * Pinned here:
 *  1. the built-in default is a Positron style from OpenFreeMap (no API key
 *     needed);
 *  2. NEXT_PUBLIC_MAP_TILE_URL override wins and trims whitespace;
 *  3. the failure tracker latches exactly at the threshold so repeated tile
 *     errors flip the UI fallback once;
 *  4. only fetch-shaped errors drive the tile-fallback panel.
 */
import { describe, expect, it } from "vitest";
import {
  MAP_TILE_URL,
  TILE_FAILURE_THRESHOLD,
  createTileFailureTracker,
  isTileFetchError,
  resolveMapTileUrl,
} from "@/lib/mapTiles";

describe("tile URL config", () => {
  it("defaults to OpenFreeMap Positron (no API key)", () => {
    const resolved = resolveMapTileUrl(undefined);
    expect(resolved).toContain("tiles.openfreemap.org");
    expect(resolved).toContain("styles/positron");
    expect(MAP_TILE_URL).toBe(resolved);
  });

  it("honours the env override over the default", () => {
    expect(resolveMapTileUrl(undefined)).toBe(
      "https://tiles.openfreemap.org/styles/positron"
    );
    expect(resolveMapTileUrl("   ")).toBe(
      "https://tiles.openfreemap.org/styles/positron"
    );
    expect(resolveMapTileUrl("  https://tiles.example.vn/{z}/{x}/{y}.png ")).toBe(
      "https://tiles.example.vn/{z}/{x}/{y}.png"
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
