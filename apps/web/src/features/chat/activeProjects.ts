/**
 * Active-project catalogue for the ProjectPicker (story 10.3).
 *
 * The backend 422 PROJECT_SCOPE error carries no project list and there is no
 * GET /api/projects endpoint yet, so the picker needs a best-effort source.
 * Order of preference (all FE-side, never touching backend files):
 *   1. a `projects` array in the 422 error body, if the backend ever includes it;
 *   2. a best-effort GET /api/projects, forward-compatible with the future
 *      endpoint;
 *   3. a static catalogue grounded in db/seed/project_config.sql, where both
 *      camellia and soleil are seeded as status = 'active'.
 *
 * The static fallback keeps the multi-project flow usable today and is replaced
 * automatically as soon as the backend serves the real list.
 */

/** One pickable active project shown in the ProjectPicker list. */
export interface ActiveProject {
  project_key: string;
  /** Commercial name shown as the primary label (ten_thuong_mai). */
  ten_thuong_mai: string;
  /** Legal name, secondary line when present (ten_phap_ly). */
  ten_phap_ly?: string;
  /** Location, secondary line when present (vi_tri). */
  vi_tri?: string;
}

/** Static catalogue grounded in db/seed/project_config.sql (status = 'active'). */
export const FALLBACK_ACTIVE_PROJECTS: ActiveProject[] = [
  {
    project_key: "camellia",
    ten_thuong_mai: "The Camellia",
    ten_phap_ly: "Khu biệt thự và nhà phố cao cấp Camellia",
    vi_tri: "Long An",
  },
  {
    project_key: "soleil",
    ten_thuong_mai: "Soleil",
    ten_phap_ly: "Chung cư cao cấp Soleil",
    vi_tri: "TP.HCM",
  },
];

/** Reads a project list from a 422 error body when the backend provides one. */
function projectsFromErrorBody(errorBody: unknown): ActiveProject[] | null {
  if (typeof errorBody !== "object" || errorBody === null) return null;
  const projects = (errorBody as { projects?: unknown }).projects;
  if (!Array.isArray(projects)) return null;
  const mapped: (ActiveProject | null)[] = projects.map((p) => {
    if (typeof p !== "object" || p === null) return null;
    const row = p as Record<string, unknown>;
    const projectKey = row.project_key ?? row.projectKey;
    const name = row.ten_thuong_mai ?? row.name;
    if (typeof projectKey !== "string" || typeof name !== "string") return null;
    return {
      project_key: projectKey,
      ten_thuong_mai: name,
      ten_phap_ly: typeof row.ten_phap_ly === "string" ? row.ten_phap_ly : undefined,
      vi_tri: typeof row.vi_tri === "string" ? row.vi_tri : undefined,
    };
  });
  const filtered = mapped.filter((p): p is ActiveProject => p !== null);
  return filtered.length > 0 ? filtered : null;
}

/** Fetches active projects from the future list endpoint, if it exists. */
async function fetchProjectList(): Promise<ActiveProject[] | null> {
  try {
    const res = await fetch("/api/projects", {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { projects?: unknown };
    return projectsFromErrorBody({ projects: data.projects });
  } catch {
    // Endpoint not implemented yet or network failure: the static catalogue
    // below keeps the multi-project flow usable.
    return null;
  }
}

/**
 * Returns the active projects for the picker. Prefers a list supplied in the
 * 422 body, then the list endpoint, and finally the static seed-grounded
 * catalogue.
 */
export async function loadActiveProjects(errorBody?: unknown): Promise<ActiveProject[]> {
  if (errorBody !== undefined) {
    const fromError = projectsFromErrorBody(errorBody);
    if (fromError) return fromError;
  }
  const fromEndpoint = await fetchProjectList();
  if (fromEndpoint) return fromEndpoint;
  return FALLBACK_ACTIVE_PROJECTS;
}
