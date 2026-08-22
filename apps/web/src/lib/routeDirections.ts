/**
 * Client-side route directions for MapPanel (story 10.7).
 *
 * The visitor's coordinates are resolved through the Geolocation API only
 * after explicit consent in a popup, are never sent to the app backend and
 * are never persisted (PDPL: precise location is personal data). Routing uses
 * the public OSRM demo server; every failure path (permission denied,
 * geolocation unsupported, HTTP error, timeout) falls back to the story 5.2
 * Google Maps deep-link that carries no origin coordinate.
 */

export interface LineStringGeometry {
  type: "LineString";
  coordinates: [number, number][];
}

export interface RouteDirections {
  status: "route";
  geometry: LineStringGeometry;
  /** Total route length in kilometers, rounded to one decimal. */
  distanceKm: number;
  /** Estimated travel time in minutes, ceiling of OSRM seconds. */
  durationMin: number;
  /** Human label, e.g. "2,4 km · 12 phút" (VN decimal separator). */
  statsLabel: string;
}

export interface FallbackDirections {
  status: "fallback";
  /** Google Maps dir deep-link, no origin coordinate. */
  url: string;
}

export type DirectionsOutcome = RouteDirections | FallbackDirections;

export interface GeolocationPositionLike {
  coords: { latitude: number; longitude: number };
}

export interface GeolocationErrorLike {
  code: number;
  message: string;
}

export interface GeolocationLike {
  getCurrentPosition(
    success: (position: GeolocationPositionLike) => void,
    error: (error: GeolocationErrorLike) => void,
    options?: PositionOptions
  ): void;
}

export interface DirectionsFetchDeps {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface DirectionsFlowDeps extends DirectionsFetchDeps {
  geolocation?: GeolocationLike;
}

export interface DirectionsFlow {
  /** User tapped "Chỉ đường": only the consent popup opens. Never requests a
   *  location here, so a page load can never fire the browser prompt. */
  begin(): void;
  /** User accepted in the popup: resolve the location, then route or fall back. */
  confirm(): Promise<DirectionsOutcome>;
  /** User dismissed the popup: keep the 5.2 deep-link behaviour. */
  cancel(): DirectionsOutcome;
}

const OSRM_BASE = "https://router.project-osrm.org/route/v1/driving";
const DEFAULT_TIMEOUT_MS = 5000;

/** Deep-link used when the browser has no origin coordinate (permission
 *  denied, geolocation unsupported or OSRM failed). */
export function buildFallbackDirectionsUrl(lat: number, lng: number): string {
  const destination = encodeURIComponent(`${lat},${lng}`);
  return `https://www.google.com/maps/dir/?api=1&destination=${destination}`;
}

/** OSRM public demo URL: source is the visitor, destination is the project. */
export function buildOsrmUrl(
  user: { lat: number; lng: number },
  project: { lat: number; lng: number }
): string {
  return `${OSRM_BASE}/${user.lng},${user.lat};${project.lng},${project.lat}?overview=full&geometries=geojson`;
}

/** Comma decimal separator is the VN locale convention for distances. */
export function formatRouteStats(distanceKm: number, durationMin: number): string {
  const distance = distanceKm.toLocaleString("vi-VN", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  return `${distance} km · ${durationMin} phút`;
}

/** Extracts the drivable route from an OSRM response; null when unusable. */
export function parseOsrmRoute(json: unknown): RouteDirections | null {
  if (typeof json !== "object" || json === null) return null;
  const body = json as { code?: unknown; routes?: unknown };
  if (body.code !== "Ok") return null;
  if (!Array.isArray(body.routes) || body.routes.length === 0) return null;
  const route = body.routes[0] as {
    distance?: unknown;
    duration?: unknown;
    geometry?: unknown;
  };
  if (typeof route.distance !== "number" || typeof route.duration !== "number") return null;
  const geometry = route.geometry as LineStringGeometry | undefined;
  if (!geometry || geometry.type !== "LineString") return null;
  if (!Array.isArray(geometry.coordinates) || geometry.coordinates.length < 2) return null;
  const distanceKm = Math.round((route.distance / 1000) * 10) / 10;
  const durationMin = Math.max(1, Math.ceil(route.duration / 60));
  return {
    status: "route",
    geometry,
    distanceKm,
    durationMin,
    statsLabel: formatRouteStats(distanceKm, durationMin),
  };
}

/** Promise wrapper around the Geolocation API so grant/deny paths can be
 *  mocked with plain functions in tests. */
export function requestUserLocation(
  geolocation: GeolocationLike | undefined
): Promise<GeolocationPositionLike> {
  return new Promise((resolve, reject) => {
    if (!geolocation) {
      reject(new Error("Geolocation is not supported"));
      return;
    }
    geolocation.getCurrentPosition(resolve, reject);
  });
}

/** Fetches the OSRM route with a hard timeout. Throws on any failure so the
 *  caller can fall back to the deep-link. */
export async function fetchRouteGeometry(
  url: string,
  deps: DirectionsFetchDeps = {}
): Promise<RouteDirections> {
  const { fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS } = deps;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`OSRM request failed with status ${response.status}`);
    }
    const route = parseOsrmRoute(await response.json());
    if (!route) throw new Error("OSRM response contained no usable route");
    return route;
  } finally {
    clearTimeout(timer);
  }
}

/** State machine for the consent-first directions flow. begin() is the only
 *  entry a component may call before the user sees the popup; geolocation is
 *  deferred to confirm() so the browser prompt never appears on page load. */
export function createDirectionsFlow(
  project: { lat: number; lng: number },
  deps: DirectionsFlowDeps = {}
): DirectionsFlow {
  const fallbackUrl = buildFallbackDirectionsUrl(project.lat, project.lng);
  return {
    begin() {
      // Consent gate only; the actual location request is deferred to
      // confirm() on purpose (popup-first, never auto-ask).
    },
    async confirm(): Promise<DirectionsOutcome> {
      try {
        const position = await requestUserLocation(deps.geolocation);
        const user = { lat: position.coords.latitude, lng: position.coords.longitude };
        const url = buildOsrmUrl(user, project);
        try {
          return await fetchRouteGeometry(url, deps);
        } catch {
          return { status: "fallback", url: fallbackUrl };
        }
      } catch {
        // Permission denied, geolocation unsupported or an unexpected error:
        // keep the 5.2 deep-link behaviour.
        return { status: "fallback", url: fallbackUrl };
      }
    },
    cancel(): DirectionsOutcome {
      return { status: "fallback", url: fallbackUrl };
    },
  };
}
