import { describe, expect, it, vi } from "vitest";
import {
  buildFallbackDirectionsUrl,
  buildOsrmUrl,
  createDirectionsFlow,
  fetchRouteGeometry,
  formatRouteStats,
  parseOsrmRoute,
  type DirectionsFetchDeps,
  type GeolocationLike,
  type GeolocationPositionLike,
  type RouteDirections,
} from "@/lib/routeDirections";

// Story 10.7 directions flow. All assertions run in the node vitest
// environment (no DOM), so the flow is driven through its plain-function
// dependencies: a mock Geolocation API for grant/deny and a mock fetch for
// OSRM success/failure. The route result carries the geometry + badge label
// that the component uses to draw the polyline on the MapLibre map.

const PROJECT = { lat: 16.1052, lng: 108.2558 };
const USER_POSITION: GeolocationPositionLike = { coords: { latitude: 16.06, longitude: 108.24 } };

const OSRM_OK: unknown = {
  code: "Ok",
  routes: [
    {
      distance: 2400,
      duration: 710,
      geometry: {
        type: "LineString",
        coordinates: [
          [108.24, 16.06],
          [108.2558, 16.1052],
        ],
      },
    },
  ],
};

function geolocationGrant(): GeolocationLike {
  return {
    getCurrentPosition: (success) => success(USER_POSITION),
  };
}

function geolocationDeny(): GeolocationLike {
  return {
    getCurrentPosition: (_success, error) => error({ code: 1, message: "User denied geolocation" }),
  };
}

function geolocationSpy(mock: GeolocationLike) {
  return vi.fn(mock.getCurrentPosition) as GeolocationLike["getCurrentPosition"];
}

function fetchResolving(json: unknown): typeof fetch {
  return vi.fn(async () => ({ ok: true, status: 200, json: async () => json })) as unknown as typeof fetch;
}

function fetchFailing(status = 500): typeof fetch {
  return vi.fn(async () => ({ ok: false, status, json: async () => ({}) })) as unknown as typeof fetch;
}

// A fetch that never settles on its own: it rejects only when the caller
// aborts it, which is how the 5s timeout inside fetchRouteGeometry surfaces.
function fetchHanging(): typeof fetch {
  return vi.fn((_url, init?: RequestInit) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new Error("Aborted")));
  })) as unknown as typeof fetch;
}

describe("buildOsrmUrl", () => {
  it("orders coordinates as user,project in lon,lat order", () => {
    const url = buildOsrmUrl({ lat: 16.06, lng: 108.24 }, PROJECT);
    expect(url).toBe(
      `https://router.project-osrm.org/route/v1/driving/108.24,16.06;108.2558,16.1052?overview=full&geometries=geojson`
    );
  });
});

describe("buildFallbackDirectionsUrl", () => {
  it("deep-links without any origin coordinate (5.2 fallback)", () => {
    const url = buildFallbackDirectionsUrl(16.1052, 108.2558);
    expect(url).toMatch(/^https:\/\/www\.google\.com\/maps\/dir\/\?api=1&destination=/);
    expect(url).not.toContain("origin");
  });
});

describe("formatRouteStats", () => {
  it("uses the VN comma decimal separator and minutes", () => {
    expect(formatRouteStats(2.4, 12)).toBe("2,4 km · 12 phút");
  });

  it("rounds to one decimal and floors a zero distance label", () => {
    expect(formatRouteStats(0, 1)).toBe("0,0 km · 1 phút");
  });
});

describe("parseOsrmRoute", () => {
  it("extracts geometry, rounded distance and ceil'd minutes", () => {
    const route = parseOsrmRoute(OSRM_OK) as RouteDirections;
    expect(route.status).toBe("route");
    expect(route.geometry.type).toBe("LineString");
    expect(route.geometry.coordinates).toHaveLength(2);
    expect(route.distanceKm).toBe(2.4);
    expect(route.durationMin).toBe(12); // 710s -> ceil(11.83) = 12
    expect(route.statsLabel).toBe("2,4 km · 12 phút");
  });

  it("returns null for a non-Ok code", () => {
    expect(parseOsrmRoute({ code: "NoRoute", routes: OSRM_OK })).toBeNull();
  });

  it("returns null when routes are missing or empty", () => {
    expect(parseOsrmRoute({ code: "Ok" })).toBeNull();
    expect(parseOsrmRoute({ code: "Ok", routes: [] })).toBeNull();
  });

  it("returns null when the geometry is unusable", () => {
    const bad = { code: "Ok", routes: [{ distance: 1, duration: 1, geometry: null }] };
    expect(parseOsrmRoute(bad)).toBeNull();
  });
});

describe("fetchRouteGeometry", () => {
  it("resolves the parsed route when OSRM answers ok", async () => {
    const fetchImpl = fetchResolving(OSRM_OK);
    const route = await fetchRouteGeometry("https://example.test/route", { fetchImpl });
    expect(route.status).toBe("route");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("throws when OSRM answers with a failing status", async () => {
    const fetchImpl = fetchFailing(502);
    await expect(fetchRouteGeometry("https://example.test/route", { fetchImpl })).rejects.toThrow(
      /status 502/
    );
  });

  it("throws when OSRM answers with an unusable body", async () => {
    const fetchImpl = fetchResolving({ code: "InvalidQuery" });
    await expect(fetchRouteGeometry("https://example.test/route", { fetchImpl })).rejects.toThrow();
  });

  it("aborts after the configured timeout instead of hanging", async () => {
    const fetchImpl = fetchHanging();
    await expect(
      fetchRouteGeometry("https://example.test/route", { fetchImpl, timeoutMs: 10 })
    ).rejects.toThrow();
  });
});

describe("createDirectionsFlow — consent-first ordering", () => {
  it("never requests the location on creation or on begin()", () => {
    const getPosition = geolocationSpy(geolocationGrant());
    const flow = createDirectionsFlow(PROJECT, { geolocation: { getCurrentPosition: getPosition } });
    flow.begin();
    expect(getPosition).not.toHaveBeenCalled();
  });

  it("requests the location only after confirm(), exactly once", async () => {
    const getPosition = geolocationSpy(geolocationGrant());
    const flow = createDirectionsFlow(PROJECT, { geolocation: { getCurrentPosition: getPosition } });
    flow.begin();
    expect(getPosition).not.toHaveBeenCalled();
    const outcome = await flow.confirm();
    expect(getPosition).toHaveBeenCalledTimes(1);
    expect(outcome.status).toBe("route");
  });

  it("returns the deep-link fallback when permission is denied and never fetches OSRM", async () => {
    const getPosition = geolocationSpy(geolocationDeny());
    const fetchImpl = vi.fn();
    const flow = createDirectionsFlow(PROJECT, {
      geolocation: { getCurrentPosition: getPosition },
      fetchImpl: fetchImpl as unknown as DirectionsFetchDeps["fetchImpl"],
    });
    const outcome = await flow.confirm();
    expect(outcome.status).toBe("fallback");
    expect(fetchImpl).not.toHaveBeenCalled();
    if (outcome.status === "fallback") {
      expect(outcome.url).toContain(`destination=${encodeURIComponent("16.1052,108.2558")}`);
    }
  });

  it("falls back when geolocation is unsupported (no navigator.geolocation)", async () => {
    const flow = createDirectionsFlow(PROJECT, { geolocation: undefined });
    const outcome = await flow.confirm();
    expect(outcome.status).toBe("fallback");
  });
});

describe("createDirectionsFlow — OSRM success and failure", () => {
  it("draws a route (geometry + badge label) when OSRM succeeds", async () => {
    const flow = createDirectionsFlow(PROJECT, {
      geolocation: geolocationGrant(),
      fetchImpl: fetchResolving(OSRM_OK),
    });
    const outcome = await flow.confirm();
    expect(outcome.status).toBe("route");
    if (outcome.status === "route") {
      expect(outcome.geometry.type).toBe("LineString");
      expect(outcome.geometry.coordinates.length).toBeGreaterThanOrEqual(2);
      expect(outcome.statsLabel).toBe("2,4 km · 12 phút");
    }
  });

  it("calls OSRM with the resolved user position as the source", async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => OSRM_OK })) as unknown as typeof fetch;
    const flow = createDirectionsFlow(PROJECT, {
      geolocation: geolocationGrant(),
      fetchImpl,
    });
    await flow.confirm();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url] = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0] as [string];
    expect(url).toContain(`/driving/108.24,16.06;108.2558,16.1052?`);
  });

  it("falls back to the deep-link when OSRM fails with an HTTP error", async () => {
    const flow = createDirectionsFlow(PROJECT, {
      geolocation: geolocationGrant(),
      fetchImpl: fetchFailing(503),
    });
    const outcome = await flow.confirm();
    expect(outcome.status).toBe("fallback");
    if (outcome.status === "fallback") expect(outcome.url).toContain("google.com/maps/dir");
  });

  it("falls back to the deep-link when OSRM times out", async () => {
    const flow = createDirectionsFlow(PROJECT, {
      geolocation: geolocationGrant(),
      fetchImpl: fetchHanging(),
      timeoutMs: 10,
    });
    const outcome = await flow.confirm();
    expect(outcome.status).toBe("fallback");
    if (outcome.status === "fallback") expect(outcome.url).toContain("google.com/maps/dir");
  });
});

describe("createDirectionsFlow — cancel", () => {
  it("returns the deep-link fallback without touching geolocation or OSRM", () => {
    const getPosition = geolocationSpy(geolocationGrant());
    const fetchImpl = vi.fn();
    const flow = createDirectionsFlow(PROJECT, {
      geolocation: { getCurrentPosition: getPosition },
      fetchImpl: fetchImpl as unknown as DirectionsFetchDeps["fetchImpl"],
    });
    const outcome = flow.cancel();
    expect(outcome.status).toBe("fallback");
    expect(getPosition).not.toHaveBeenCalled();
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
