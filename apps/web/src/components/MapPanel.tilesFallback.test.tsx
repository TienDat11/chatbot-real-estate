// @vitest-environment jsdom
/**
 * Offline-tile fallback panel tests (mocked maplibre-gl events).
 *
 * The dev/prod network can block the tile CDN; maplibre then surfaces each
 * failed request as an "error" event and the canvas stays gray forever.
 * Pinned here:
 *  1. the default tile source handed to maplibre is the configured provider
 *     (expanded CARTO URLs + correct attribution), not plain OSM direct;
 *  2. fewer than TILE_FAILURE_THRESHOLD fetch errors keep the fallback hidden;
 *  3. reaching the threshold swaps in a clean panel with the project name,
 *     address and an antd notice — no layout shift (absolute overlay);
 *  4. unrelated non-fetch map errors never trigger it.
 */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TILE_FAILURE_THRESHOLD } from "@/lib/mapTiles";

interface FakeMapInstance {
  opts: unknown;
  errorHandlers: Array<(e: unknown) => void>;
  removed: { layers: string[]; sources: string[] };
}

const harness = vi.hoisted(() => ({ maps: [] as FakeMapInstance[] }));

vi.mock("maplibre-gl", () => {
  class FakeMap {
    opts: unknown;
    errorHandlers: Array<(e: unknown) => void> = [];
    removed = { layers: [] as string[], sources: [] as string[] };
    constructor(opts: unknown) {
      this.opts = opts;
      harness.maps.push(this);
    }
    on(type: string, handler: (e: unknown) => void) {
      if (type === "error") this.errorHandlers.push(handler);
    }
    flyTo() {}
    getLayer(id: string) {
      return this.removed.layers.includes(id) ? undefined : {};
    }
    getSource(id: string) {
      return this.removed.sources.includes(id) ? undefined : {};
    }
    removeLayer(id: string) {
      this.removed.layers.push(id);
    }
    removeSource(id: string) {
      this.removed.sources.push(id);
    }
    isStyleLoaded() {
      return true;
    }
    addSource() {}
    addLayer() {}
    fitBounds() {}
    remove() {}
  }
  class FakeMarker {
    setLngLat() {
      return this;
    }
    addTo() {
      return this;
    }
    remove() {}
  }
  class FakePopup {
    setLngLat() {
      return this;
    }
    setDOMContent() {
      return this;
    }
    addTo() {
      return this;
    }
    remove() {}
  }
  class FakeLngLatBounds {
    extend() {}
  }
  return { Map: FakeMap, Marker: FakeMarker, Popup: FakePopup, LngLatBounds: FakeLngLatBounds };
});

import { MapPanel } from "@/components/MapPanel";

const PROJECT = {
  lat: 16.0710756,
  lng: 108.2436243,
  name: "The Soleil Đà Nẵng",
  address: "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
};

interface FakeMapHandle {
  opts: { style: { sources: { osm: { tiles: string[]; attribution: string } } } };
  errorHandlers: Array<(e: unknown) => void>;
}

async function mountPanel(): Promise<FakeMapHandle> {
  render(<MapPanel places={[]} project={PROJECT} />);
  await waitFor(() => expect(harness.maps.length).toBeGreaterThan(0));
  return harness.maps[harness.maps.length - 1] as FakeMapHandle;
}

function failAllFetchTiles(map: FakeMapHandle) {
  act(() => {
    map.errorHandlers.forEach((h) =>
      h({ error: { status: 0, message: "AJAXError: Failed to fetch" } })
    );
  });
}

afterEach(() => {
  cleanup();
  harness.maps.length = 0;
});

describe("MapPanel offline tile fallback", () => {
  it("serves the configured default tiles (expanded CARTO URLs), not OSM direct", async () => {
    const map = await mountPanel();
    const source = map.opts.style.sources.osm;
    expect(source.tiles).toHaveLength(4);
    source.tiles.forEach((url) => {
      expect(url).toContain("basemaps.cartocdn.com");
      expect(url).not.toContain("{s}");
    });
    expect(source.attribution).toContain("CARTO");
  });

  it("keeps the fallback hidden below the threshold and shows it at the threshold", async () => {
    const map = await mountPanel();
    expect(map.errorHandlers.length).toBeGreaterThan(0);
    for (let i = 1; i < TILE_FAILURE_THRESHOLD; i += 1) {
      failAllFetchTiles(map);
      expect(screen.queryByText("Bản đồ tạm thời không tải được")).toBeNull();
    }
    failAllFetchTiles(map);
    expect(await screen.findByText("Bản đồ tạm thời không tải được")).toBeTruthy();
    // The panel carries the project identity while tiles are unavailable.
    expect(screen.getByText(PROJECT.name)).toBeTruthy();
    expect(screen.getByText(PROJECT.address)).toBeTruthy();
  });

  it("never triggers on unrelated (non-fetch) map errors", async () => {
    const map = await mountPanel();
    for (let i = 0; i < TILE_FAILURE_THRESHOLD + 2; i += 1) {
      act(() => {
        map.errorHandlers.forEach((h) => h({ error: new Error("layers[0]: style parse issue") }));
      });
    }
    expect(screen.queryByText("Bản đồ tạm thời không tải được")).toBeNull();
  });
});
