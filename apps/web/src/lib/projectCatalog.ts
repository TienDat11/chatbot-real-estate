/**
 * Adapter for GET /api/projects (active project catalogue).
 *
 * Why an adapter instead of fetching inline: the URL-routing feature keys the
 * whole chat off this endpoint, so shape drift must be fixable in ONE file.
 * The mapper accepts the current contract
 * `{"projects":[{"project_key","display_name"}]}` plus every legacy alias the
 * backend has shipped (`name`, `ten_thuong_mai`, `vi_tri`) so a field rename
 * never breaks routing or the picker.
 */

/** Minimal project identity the router and gate need. */
export interface ProjectSummary {
  project_key: string;
  /** Commercial name shown to customers (contract `display_name`). */
  display_name: string;
  /** Compact header label (contract `short_name`, e.g. "The Soleil"). */
  short_name?: string;
  location?: string;
  lat?: number;
  lng?: number;
  is_hot?: boolean;
}

/** Raised when the catalogue is missing, malformed or unreachable for good. */
export class ProjectCatalogUnavailableError extends Error {
  constructor(cause?: unknown) {
    super("Danh sách dự án tạm thời không khả dụng.", { cause });
    this.name = "ProjectCatalogUnavailableError";
  }
}

const PROJECTS_ENDPOINT = "/api/projects";

/**
 * Maps one raw catalogue row to a ProjectSummary; null when the row cannot
 * identify a project (missing key or every name alias absent). Non-string
 * optional fields are dropped rather than trusted.
 */
function mapProjectRow(row: unknown): ProjectSummary | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const key = typeof r.project_key === "string" ? r.project_key : "";
  const name =
    firstNonEmptyString(r.display_name) ?? firstNonEmptyString(r.name) ?? firstNonEmptyString(r.ten_thuong_mai);
  if (!key || !name) return null;
  return {
    project_key: key,
    display_name: name,
    short_name: firstNonEmptyString(r.short_name),
    location: firstNonEmptyString(r.location, r.vi_tri),
    lat: typeof r.lat === "number" ? r.lat : undefined,
    lng: typeof r.lng === "number" ? r.lng : undefined,
    is_hot: typeof r.is_hot === "boolean" ? r.is_hot : undefined,
  };
}

function firstNonEmptyString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
  }
  return undefined;
}

/**
 * Pure mapper from the endpoint payload to the domain list. Throws
 * ProjectCatalogUnavailableError when the payload carries no usable project —
 * callers must treat an empty catalogue as "cannot route", never as license
 * to invent a default.
 */
export function parseProjectCatalog(payload: unknown): ProjectSummary[] {
  const rows = (payload as { projects?: unknown } | null | undefined)?.projects;
  if (!Array.isArray(rows)) throw new ProjectCatalogUnavailableError();
  const mapped = rows.map(mapProjectRow).filter((p): p is ProjectSummary => p !== null);
  if (mapped.length === 0) throw new ProjectCatalogUnavailableError();
  return mapped;
}

export interface FetchProjectCatalogOptions {
  /** Extra attempts after the first failure (default 2 → 3 tries total). */
  retries?: number;
  /** Per-attempt fetch timeout in ms (default 3000). */
  timeoutMs?: number;
  /** Wait between attempts in ms (default 400; 0 keeps tests synchronous). */
  retryDelayMs?: number;
}

// In-flight dedupe: the root gate and ChatPage both resolve the catalogue on
// mount (and dev StrictMode mounts twice), so concurrent callers share one
// network round trip without caching stale data across page loads.
let inflight: Promise<ProjectSummary[]> | null = null;

/**
 * Fetches the active-project catalogue with bounded retries. Rejects with
 * ProjectCatalogUnavailableError only after every attempt fails — callers then
 * show a recoverable error state instead of guessing a default project.
 */
export async function fetchProjectCatalog(options: FetchProjectCatalogOptions = {}): Promise<ProjectSummary[]> {
  if (inflight) return inflight;
  inflight = fetchWithRetry(options).finally(() => {
    inflight = null;
  });
  return inflight;
}

async function fetchWithRetry(options: FetchProjectCatalogOptions): Promise<ProjectSummary[]> {
  const retries = options.retries ?? 2;
  const timeoutMs = options.timeoutMs ?? 3000;
  const retryDelayMs = options.retryDelayMs ?? 400;
  let lastCause: unknown;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(PROJECTS_ENDPOINT, {
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!response.ok) throw new ProjectCatalogUnavailableError();
      return parseProjectCatalog(await response.json());
    } catch (cause) {
      lastCause = cause;
      if (attempt < retries && retryDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
      }
    }
  }
  throw lastCause instanceof ProjectCatalogUnavailableError
    ? lastCause
    : new ProjectCatalogUnavailableError(lastCause);
}
