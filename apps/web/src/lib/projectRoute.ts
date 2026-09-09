/**
 * Pure redirect decisions for the project-scoped URL scheme
 * (/project/[projectKey]). No React, no fetch: every rule the root gate and
 * the routed chat page apply is a plain function over (catalogue, input) so
 * vitest can pin the contract — bare "/" goes to the FIRST backend entry,
 * legacy ?project_key links convert to path form, and unknown keys fall back
 * to that same first entry instead of a silent hardcoded default.
 */

import type { ProjectSummary } from "@/lib/projectCatalog";

export const PROJECT_PATH_PREFIX = "/project";

/** Builds the canonical chat URL for one project key. */
export function projectPath(projectKey: string): string {
  return `${PROJECT_PATH_PREFIX}/${encodeURIComponent(projectKey)}`;
}

/** Outcome of resolving GET "/" against a resolved catalogue. */
export type BareRootDecision =
  | { kind: "redirect"; path: string }
  /** Legacy ?project_key=<key> whose key is not active any more. */
  | { kind: "legacy_unknown"; fallbackPath: string };

/**
 * Decides where the bare root "/" must go. `catalog` is the backend list in
 * the contract order (default project FIRST, then the remaining actives
 * hot-first by name), so catalog[0] IS the default — never guessed
 * client-side. A valid legacy ?project_key converts to the path form; an
 * invalid one still leaves "/" but lands on the default with a notice.
 */
export function decideBareRootRedirect(
  catalog: ProjectSummary[],
  search: string | URLSearchParams
): BareRootDecision {
  const params = search instanceof URLSearchParams ? search : new URLSearchParams(search);
  const defaultPath = projectPath(catalog[0].project_key);
  const legacyKey = params.get("project_key");
  if (legacyKey === null || legacyKey.length === 0) {
    return { kind: "redirect", path: defaultPath };
  }
  return isKnownProject(catalog, legacyKey)
    ? { kind: "redirect", path: projectPath(legacyKey) }
    : { kind: "legacy_unknown", fallbackPath: defaultPath };
}

/** Outcome of validating one /project/[projectKey] segment. */
export type RoutedProjectDecision =
  | { kind: "accept" }
  | { kind: "unknown"; fallbackPath: string };

/**
 * Validates a routed project key against the catalogue. Unknown keys must
 * redirect to the default project with a notice — chatting against a dead
 * scope would silently answer from the wrong corpus.
 */
export function decideRoutedProject(catalog: ProjectSummary[], projectKey: string): RoutedProjectDecision {
  return isKnownProject(catalog, projectKey)
    ? { kind: "accept" }
    : { kind: "unknown", fallbackPath: projectPath(catalog[0].project_key) };
}

function isKnownProject(catalog: ProjectSummary[], projectKey: string): boolean {
  return catalog.some((p) => p.project_key === projectKey);
}
