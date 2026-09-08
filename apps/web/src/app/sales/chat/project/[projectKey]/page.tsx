import { SalesChatShell } from "@/features/sales/SalesChatShell";
import { resolveSessionParam } from "@/lib/canonicalProjectUrl";

/** Decodes the URL segment; a malformed escape falls back to the raw value. */
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Canonical project-scoped sales chat surface: the URL segment is the single
 * source of truth for the active project and `?sessionId=` deep-links a
 * persisted transcript. The persistent sales layout owns the shell; the
 * shared ChatPage canvas owns project switching, history and streaming.
 */
export default async function SalesProjectChatPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectKey: string }>;
  searchParams: Promise<{ sessionId?: string | string[]; session?: string | string[] }>;
}) {
  const { projectKey } = await params;
  // Canonical `sessionId` wins; legacy `?session=` deep links still hydrate.
  const sessionId = resolveSessionParam(await searchParams);
  return <SalesChatShell routeProjectKey={safeDecode(projectKey)} sessionId={sessionId} />;
}
