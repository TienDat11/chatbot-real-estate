/**
 * Single source of truth for the basemap style the chat map renders.
 *
 * Why the default is NOT plain OSM: the OSM tile servers are frequently
 * unreachable from Vietnam networks, which left the map a blank gray canvas
 * with console AJAXErrors. The default below serves OpenStreetMap data from
 * OpenFreeMap (Positron, no API key required, attribution embedded in the
 * style); deployments can point anywhere else via NEXT_PUBLIC_MAP_TILE_URL.
 */

/** Env-overridable MapLibre style URL. */
export function resolveMapTileUrl(raw?: string): string {
  const trimmed = raw?.trim();
  return trimmed ? trimmed : "https://tiles.openfreemap.org/styles/positron";
}

/** Resolved config consumed by MapPanel (env read stays inline for Next build inlining). */
export const MAP_TILE_URL = resolveMapTileUrl(process.env.NEXT_PUBLIC_MAP_TILE_URL);

/* ---- repeated tile failures -> graceful fallback ---- */

/**
 * One failed tile is noise (a flaky CDN shard); many consecutive failures mean
 * the provider is unreachable and the map will stay blank. Six failures before
 * showing the fallback panel keeps false positives negligible.
 */
export const TILE_FAILURE_THRESHOLD = 6;

export interface TileFailureTracker {
  /** Records one failure; true exactly when the threshold is (or was) reached. */
  record(): boolean;
  /** Clears the count (fresh map instance starts over). */
  reset(): void;
}

export function createTileFailureTracker(threshold: number = TILE_FAILURE_THRESHOLD): TileFailureTracker {
  let failures = 0;
  return {
    record() {
      if (failures >= threshold) return true;
      failures += 1;
      return failures === threshold;
    },
    reset() {
      failures = 0;
    },
  };
}

interface TileErrorInfo {
  status?: number;
  message?: string;
}

function extractErrorInfo(event: unknown): TileErrorInfo | null {
  if (typeof event !== "object" || event === null) return null;
  // maplibre wraps the cause in an ErrorEvent ({ error }); some paths pass the
  // error object itself, so accept both shapes.
  const outer = event as { error?: unknown; status?: unknown; message?: unknown };
  const candidate = outer.error ?? outer;
  if (typeof candidate !== "object" || candidate === null) return null;
  const info = candidate as { status?: unknown; message?: unknown };
  return {
    status: typeof info.status === "number" ? info.status : undefined,
    message: typeof info.message === "string" ? info.message : undefined,
  };
}

/**
 * Only fetch-shaped errors should drive the fallback: network failures
 * (status 0), HTTP >= 400 responses, or fetch rejection messages without a
 * status. Unrelated style/parsing errors never trigger it.
 */
export function isTileFetchError(event: unknown): boolean {
  const info = extractErrorInfo(event);
  if (!info) return false;
  if (typeof info.status === "number") return info.status === 0 || info.status >= 400;
  return /failed to fetch|ajaxerror|network\s*error/i.test(info.message ?? "");
}
