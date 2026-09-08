/**
 * Cross-project redirect guardrail (FE half): when a question asked in project
 * X is really about project Y, the backend attaches an optional
 * `project_redirect` field to the SSE `done` payload. The FE renders a
 * "switch project" card under the answer linking to /project/<key>.
 *
 * Why a normalizer instead of trusting the cast: the field is optional and the
 * backend ships it incrementally, so every malformed/absent shape must degrade
 * to "no card", never crash the chat.
 */

/** Domain shape consumed by the redirect card. */
export interface ProjectRedirect {
  project_key: string;
  /** Commercial name for the CTA label; falls back to the key. */
  displayName: string;
  /** Compact name preferred for the CTA label when the backend sends one. */
  shortName?: string;
}

/**
 * Validates one raw `project_redirect` value. Returns null for anything that
 * cannot identify a destination project (absent, non-object, empty key) so
 * callers can spread it conditionally into message state.
 */
export function normalizeProjectRedirect(raw: unknown): ProjectRedirect | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;
  const key =
    typeof r.project_key === "string" && r.project_key.length > 0
      ? r.project_key
      : typeof r.projectKey === "string" && r.projectKey.length > 0
        ? r.projectKey
        : "";
  if (!key) return null;
  const displayName = firstNonEmptyString(r.display_name, r.displayName) ?? key;
  const shortName = firstNonEmptyString(r.short_name, r.shortName);
  return shortName ? { project_key: key, displayName, shortName } : { project_key: key, displayName };
}

function firstNonEmptyString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
  }
  return undefined;
}
