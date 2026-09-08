/** Canonical session-id query param for shareable project routes. */
const CANONICAL_SESSION_PARAM = "sessionId";
/** Pre-R2 spelling; read for backward compatibility, never written. */
const LEGACY_SESSION_PARAM = "session";

/**
 * Builds the shareable project route, optionally scoped to a chat session.
 * Writes always use the canonical `sessionId` key — never the legacy one.
 */
export function canonicalProjectUrl(projectKey: string, sessionId?: string | null): string {
  const path = `/project/${encodeURIComponent(projectKey)}`;
  if (!sessionId) return path;
  return `${path}?${CANONICAL_SESSION_PARAM}=${encodeURIComponent(sessionId)}`;
}

/** Path prefix of the canonical project-scoped sales chat route. */
export const SALES_CHAT_PROJECT_PREFIX = "/sales/chat/project";

/** Canonical, shareable sales chat URL for one project, optionally session-scoped. */
export function salesChatProjectUrl(projectKey: string, sessionId?: string | null): string {
  const path = `${SALES_CHAT_PROJECT_PREFIX}/${encodeURIComponent(projectKey)}`;
  if (!sessionId) return path;
  return `${path}?${CANONICAL_SESSION_PARAM}=${encodeURIComponent(sessionId)}`;
}

/** Chat URL surface: customer storefront (/project/*) vs sales workspace. */
export type ChatSurface = "customer" | "sales";

/**
 * Mode-aware canonical project URL: the sales surface keeps every project/
 * session write on its own /sales/chat/project/* route, the customer
 * storefront stays on /project/*.
 */
export function projectChatUrl(surface: ChatSurface, projectKey: string, sessionId?: string | null): string {
  return surface === "sales"
    ? salesChatProjectUrl(projectKey, sessionId)
    : canonicalProjectUrl(projectKey, sessionId);
}

/** Route-page `searchParams` shape: Next may hand one value or an array per key. */
type SessionSearchParams = {
  sessionId?: string | string[];
  session?: string | string[];
};

function firstNonEmpty(value?: string | string[]): string | undefined {
  const raw = Array.isArray(value) ? value[0] : value;
  return raw && raw.length > 0 ? raw : undefined;
}

/**
 * Resolves the session id from route search params: the canonical `sessionId`
 * key wins; a legacy `?session=` deep link still hydrates instead of breaking.
 */
export function resolveSessionParam(
  searchParams: SessionSearchParams,
): string | undefined {
  return firstNonEmpty(searchParams.sessionId) ?? firstNonEmpty(searchParams.session);
}

/**
 * Write-side counterpart used when rewriting the current URL in place: sets
 * the canonical key and removes the legacy one so both never coexist. Other
 * query params (e.g. mode/history state) are left untouched.
 */
export function applySessionParam(params: URLSearchParams, sessionId: string): void {
  params.set(CANONICAL_SESSION_PARAM, sessionId);
  params.delete(LEGACY_SESSION_PARAM);
}
