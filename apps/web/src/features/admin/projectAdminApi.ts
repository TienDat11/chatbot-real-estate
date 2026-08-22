/**
 * Admin CMS API port (story 8.4 / ISSUE-07).
 *
 * Two reads/writes the admin screen needs: the active project catalogue
 * (GET /api/projects, live since story 10.3) and the publish trigger. The
 * publish endpoint belongs to ISSUE-13's PublishProjectWorkflow (wave 3);
 * until that ships the backend answers 404, which this port surfaces as the
 * typed `publishEndpointNotDeployed` outcome so the UI can explain instead of
 * showing a generic failure.
 */

export interface AdminProjectCatalogueEntry {
  project_key: string;
  name: string;
  location: string;
  status: string;
}

export type ProjectPublishOutcome =
  | { kind: "accepted" }
  | { kind: "publishEndpointNotDeployed" }
  | { kind: "forbidden" }
  | { kind: "unauthenticated" }
  | { kind: "network" };

/** Fetches the project catalogue; returns [] on any failure (picker contract). */
export async function fetchAdminProjectCatalogue(): Promise<AdminProjectCatalogueEntry[]> {
  try {
    const response = await fetch("/api/projects", { signal: AbortSignal.timeout(5000) });
    if (!response.ok) return [];
    const body = (await response.json()) as {
      projects?: { project_key?: string; name?: string; location?: string; status?: string }[];
    };
    return (body.projects ?? []).map((project) => ({
      project_key: String(project.project_key ?? ""),
      name: String(project.name ?? project.project_key ?? ""),
      location: String(project.location ?? ""),
      status: String(project.status ?? "active"),
    }));
  } catch {
    return [];
  }
}

/**
 * Triggers ISSUE-13's publish workflow for one project. Maps HTTP outcomes
 * onto the discriminated union so PublishButton never string-matches errors.
 */
export async function triggerProjectPublish(
  projectKey: string,
  bearerToken: string,
): Promise<ProjectPublishOutcome> {
  let response: Response;
  try {
    response = await fetch(`/api/admin/projects/${encodeURIComponent(projectKey)}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${bearerToken}` },
      body: JSON.stringify({}),
    });
  } catch {
    return { kind: "network" };
  }
  if (response.ok) return { kind: "accepted" };
  if (response.status === 404) return { kind: "publishEndpointNotDeployed" };
  if (response.status === 401) return { kind: "unauthenticated" };
  if (response.status === 403) return { kind: "forbidden" };
  return { kind: "network" };
}
