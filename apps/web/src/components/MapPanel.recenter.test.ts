import { describe, expect, it, vi } from "vitest";
import { MAP_PROJECT_ZOOM, recenterForProject, type RecenterMapLike } from "@/components/MapPanel";

// Project-change recenter (wave-1 UX): two active projects sit kilometres
// apart, so when the active project changes the shared maplibre map must drop
// the previous project's route overlay and fly the camera to the new project's
// geo center. recenterForProject is a plain function over a minimal map surface
// so vitest (node env, no DOM) can drive it with a mock map instance.

const ROUTE_SOURCE_ID = "directions-route";
const ROUTE_LAYER_ID = "directions-route-line";

function mockMap(): { flyTo: ReturnType<typeof vi.fn>; getLayer: ReturnType<typeof vi.fn>; getSource: ReturnType<typeof vi.fn>; removeLayer: ReturnType<typeof vi.fn>; removeSource: ReturnType<typeof vi.fn> } {
  return {
    flyTo: vi.fn(),
    getLayer: vi.fn(() => undefined),
    getSource: vi.fn(() => undefined),
    removeLayer: vi.fn(),
    removeSource: vi.fn(),
  };
}

describe("recenterForProject", () => {
  it("flies to the new project's geo center at the project zoom", () => {
    const map = mockMap();
    recenterForProject(map as unknown as RecenterMapLike, { lat: 16.0710756, lng: 108.2436243 });
    expect(map.flyTo).toHaveBeenCalledTimes(1);
    expect(map.flyTo).toHaveBeenCalledWith({
      center: [108.2436243, 16.0710756],
      zoom: MAP_PROJECT_ZOOM,
      duration: 800,
    });
  });

  it("removes the previous project's route layer and source before flying", () => {
    const map = mockMap();
    map.getLayer.mockReturnValue({});
    map.getSource.mockReturnValue({});
    recenterForProject(map as unknown as RecenterMapLike, { lat: 16.1052, lng: 108.2558 });
    expect(map.removeLayer).toHaveBeenCalledWith(ROUTE_LAYER_ID);
    expect(map.removeSource).toHaveBeenCalledWith(ROUTE_SOURCE_ID);
  });

  it("does not touch the route overlay when none exists", () => {
    const map = mockMap();
    recenterForProject(map as unknown as RecenterMapLike, { lat: 1, lng: 2 });
    expect(map.removeLayer).not.toHaveBeenCalled();
    expect(map.removeSource).not.toHaveBeenCalled();
  });

  it("tolerates a map surface with no route-query methods at all", () => {
    const map = { flyTo: vi.fn() };
    expect(() => recenterForProject(map as unknown as RecenterMapLike, { lat: 1, lng: 2 })).not.toThrow();
    expect(map.flyTo).toHaveBeenCalledTimes(1);
  });

  it("accepts an explicit zoom override", () => {
    const map = mockMap();
    recenterForProject(map as unknown as RecenterMapLike, { lat: 16.0, lng: 108.24 }, 15);
    expect(map.flyTo).toHaveBeenCalledWith({ center: [108.24, 16.0], zoom: 15, duration: 800 });
  });
});
