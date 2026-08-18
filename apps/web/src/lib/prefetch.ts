/**
 * In-memory prefetch cache for the map/company panels (Story 5.5 §3.2.5).
 * Fetched in the background on app mount; panels read this first and
 * revalidate when real SSE data arrives.
 */

let placesCache: unknown = null;
let placesPromise: Promise<unknown> | null = null;

export async function prefetchPlaces(): Promise<unknown> {
  if (placesCache) return placesCache;
  if (!placesPromise) {
    placesPromise = fetch("/api/places")
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null)
      .then((data) => {
        placesCache = data;
        return data;
      });
  }
  return placesPromise;
}

/** Trigger prefetches on app mount. Safe to call multiple times. */
export function warmPrefetchCache() {
  if (typeof window === "undefined") return;
  void prefetchPlaces();
}
