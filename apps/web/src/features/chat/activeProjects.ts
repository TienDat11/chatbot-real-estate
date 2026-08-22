/**
 * Active-project catalogue for the ProjectPicker (story 10.3, wave-1 UX).
 *
 * The backend 422 PROJECT_SCOPE error carries no project list, so the picker
 * needs a best-effort source. Order of preference (all FE-side, never touching
 * backend files):
 *   1. a `projects` array in the 422 error body, if the backend ever includes it;
 *   2. GET /api/projects (the wave-1 endpoint being added in parallel), shaped
 *      as `{"projects":[{"project_key","name","location","lat","lng","is_hot"}]}`
 *      already sorted with `is_hot` first;
 *   3. a static catalogue grounded in db/seed/project_config.sql + the processed
 *      project data (data/_processed/{camellia,soleil}/project_info.json), where
 *      both camellia and soleil are seeded as status = 'active'.
 *
 * The static fallback keeps the multi-project flow usable today and is replaced
 * automatically as soon as the backend serves the real list. The old catalogue
 * was placeholder copy (Camellia "Long An", Soleil "TP.HCM"); the fallback here
 * carries the verified addresses and geo centers so the picker, header chip and
 * map all stay truthful even before the endpoint exists.
 */

/** One pickable active project shown in the ProjectPicker list. */
export interface ActiveProject {
  project_key: string;
  /** Commercial name shown as the primary label (contract `name`). */
  name: string;
  /** Legacy alias carried by the 422-body catalogue; prefer `name`. */
  ten_thuong_mai?: string;
  /** Detailed address shown under the name (contract `location`). */
  location?: string;
  /** Legacy alias carried by the 422-body catalogue; prefer `location`. */
  vi_tri?: string;
  /** Legal name, secondary line when present (ten_phap_ly). */
  ten_phap_ly?: string;
  /** Geo center latitude for the map (contract `lat`). */
  lat?: number;
  /** Geo center longitude for the map (contract `lng`). */
  lng?: number;
  /** Hot project promoted to the top of the picker (contract `is_hot`). */
  is_hot?: boolean;
}

/** Display name helper: new contract field first, legacy alias second. */
export function projectDisplayName(project: ActiveProject): string {
  return project.name ?? project.ten_thuong_mai ?? project.project_key;
}

/** Display location helper: new contract field first, legacy alias second. */
export function projectDisplayLocation(project: ActiveProject): string | undefined {
  return project.location ?? project.vi_tri;
}

/**
 * Static catalogue grounded in db/seed/project_config.sql (status = 'active')
 * and data/_processed/{camellia,soleil}/project_info.json. Camellia is the hot
 * project (task wave-1) and is listed first.
 */
export const FALLBACK_ACTIVE_PROJECTS: ActiveProject[] = [
  {
    project_key: "camellia",
    name: "The Camellia Sơn Trà - Đà Nẵng",
    ten_thuong_mai: "The Camellia Sơn Trà - Đà Nẵng",
    ten_phap_ly: "Trung tâm Thương mại, văn phòng cho thuê và nhà ở cao tầng",
    location: "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng",
    vi_tri: "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng",
    lat: 16.1052,
    lng: 108.2558,
    is_hot: true,
  },
  {
    project_key: "soleil",
    name: "The Soleil Đà Nẵng",
    ten_thuong_mai: "The Soleil Đà Nẵng",
    ten_phap_ly: "Tổ hợp Ánh Dương - Soleil",
    location: "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
    vi_tri: "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
    lat: 16.0710756,
    lng: 108.2436243,
    is_hot: false,
  },
];

/**
 * Master-plan rule (story 10.1): when more than one project is active the
 * customer must ALWAYS pick explicitly. Returns true when the picker must be
 * forced on load — more than one active project and no explicit user choice
 * stored. A stored key is a user decision (ragre.project_key written by the
 * picker), never a system default, so it skips the gate. The FE must never
 * silently default to a project.
 */
export function shouldForceProjectPicker(activeProjectCount: number, storedProjectKey: string | null): boolean {
  return activeProjectCount > 1 && storedProjectKey === null;
}

/**
 * Stable picker order: is_hot projects first (Camellia above all), then by
 * display name. The GET /api/projects endpoint already sorts this way; the
 * sort here keeps every fallback source consistent with that contract.
 */
export function sortActiveProjects(projects: ActiveProject[]): ActiveProject[] {
  return [...projects].sort((a, b) => {
    const hotDiff = Number(Boolean(b.is_hot)) - Number(Boolean(a.is_hot));
    if (hotDiff !== 0) return hotDiff;
    return projectDisplayName(a).localeCompare(projectDisplayName(b), "vi");
  });
}

/** Maps one raw row (contract or legacy 422-body shape) to an ActiveProject. */
function mapProjectRow(row: unknown): ActiveProject | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const projectKey = r.project_key ?? r.projectKey;
  const name = r.name ?? r.ten_thuong_mai;
  if (typeof projectKey !== "string" || typeof name !== "string") return null;
  return {
    project_key: projectKey,
    name,
    ten_thuong_mai: typeof r.ten_thuong_mai === "string" ? r.ten_thuong_mai : undefined,
    location: typeof r.location === "string" ? r.location : undefined,
    vi_tri: typeof r.vi_tri === "string" ? r.vi_tri : undefined,
    ten_phap_ly: typeof r.ten_phap_ly === "string" ? r.ten_phap_ly : undefined,
    lat: typeof r.lat === "number" ? r.lat : undefined,
    lng: typeof r.lng === "number" ? r.lng : undefined,
    is_hot: typeof r.is_hot === "boolean" ? r.is_hot : undefined,
  };
}

/** Reads a project array from a payload; null when absent or unusable. */
function parseProjectRows(projects: unknown): ActiveProject[] | null {
  if (!Array.isArray(projects)) return null;
  const mapped = projects.map(mapProjectRow).filter((p): p is ActiveProject => p !== null);
  return mapped.length > 0 ? mapped : null;
}

/** Fetches active projects from GET /api/projects, if the endpoint exists. */
async function fetchProjectList(): Promise<ActiveProject[] | null> {
  try {
    const res = await fetch("/api/projects", {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { projects?: unknown };
    return parseProjectRows(data.projects);
  } catch {
    // Endpoint not implemented yet or network failure: the static catalogue
    // below keeps the multi-project flow usable.
    return null;
  }
}

/**
 * Returns the active projects for the picker, sorted hot-first. Prefers a list
 * supplied in the 422 body, then the list endpoint, and finally the static
 * seed-grounded catalogue. The caller decides whether to force the picker via
 * shouldForceProjectPicker() — this never picks a default silently.
 */
export async function loadActiveProjects(errorBody?: unknown): Promise<ActiveProject[]> {
  if (errorBody !== undefined) {
    const fromError = parseProjectRows((errorBody as { projects?: unknown })?.projects);
    if (fromError) return sortActiveProjects(fromError);
  }
  const fromEndpoint = await fetchProjectList();
  if (fromEndpoint) return sortActiveProjects(fromEndpoint);
  return FALLBACK_ACTIVE_PROJECTS;
}
