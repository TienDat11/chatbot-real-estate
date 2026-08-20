/**
 * Placeholder for the map/company panel prefetch cache (Story 5.5 §3.2.5).
 *
 * The previous implementation fired `fetch("/api/places")` on every app mount,
 * but no such backend route exists yet (Story 5.1 adds map panels backed by a
 * client-side static catalog, not an /api/places endpoint). Every mount produced
 * a guaranteed 404 and cached a permanent null. Disabled until a real places
 * endpoint plus a consuming panel land together.
 */

/** Trigger prefetches on app mount. Safe to call multiple times. */
export function warmPrefetchCache() {
  // No-op: no /api/places endpoint or panel consumer exists on this branch yet.
  return;
}
