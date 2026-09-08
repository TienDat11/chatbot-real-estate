"use client";

/**
 * Project cover imagery for the sales-demo UI (picker + admin catalogue).
 *
 * Reuses the existing greeting-media fetch (POST /api/llms-hello with
 * {project_key} in the JSON body) to obtain REAL project images already served
 * by the backend — never hardcoded CDN URLs. Results are cached in a module
 * map so the picker and the admin catalogue never re-fetch the same project.
 * Every failure resolves to null so callers render the graceful gradient
 * fallback instead of a broken image.
 */
import { useEffect, useState } from "react";
import { fetchGreetingMedia } from "@/lib/api";

const coverCache = new Map<string, Promise<string | null>>();

function fetchCover(projectKey: string): Promise<string | null> {
  let pending = coverCache.get(projectKey);
  if (!pending) {
    pending = fetchGreetingMedia(projectKey)
      .then((media) => {
        if (media.images.length === 0) return null;
        // Prefer the master floor plan, then any first asset.
        const master = media.images.find((im) => im.kind === "matbang");
        return (master ?? media.images[0]).url_cdn ?? null;
      })
      .catch(() => null);
    coverCache.set(projectKey, pending);
  }
  return pending;
}

/**
 * Returns the project's cover image URL, or null while loading / on failure
 * (callers render the gradient fallback). One request per project key.
 */
export function useProjectCover(projectKey: string): string | null {
  const [cover, setCover] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void fetchCover(projectKey).then((url) => {
      if (active) setCover(url);
    });
    return () => {
      active = false;
    };
  }, [projectKey]);

  return cover;
}

/** One-shot (non-hook) cover fetch, used outside React render. */
export async function projectCover(projectKey: string): Promise<string | null> {
  return fetchCover(projectKey);
}
