import { ChatPage } from "@/components/ChatPage";
import { AuthProvider } from "@/lib/AuthProvider";
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
 * Customer chat scoped to ONE project: the URL segment is the single source of
 * truth for the active project key. The server shell only awaits params
 * (Next 16 promise props) — all interactivity stays in ChatPage, which applies
 * the project-switch reset whenever this prop changes.
 */
export default async function ProjectChatPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectKey: string }>;
  searchParams: Promise<{ sessionId?: string | string[]; session?: string | string[] }>;
}) {
  const { projectKey } = await params;
  const query = await searchParams;
  // Canonical `sessionId` wins; legacy `?session=` deep links still hydrate.
  const sessionValue = resolveSessionParam(query);
  return (
    <AuthProvider>
      <ChatPage routeProjectKey={safeDecode(projectKey)} sessionId={sessionValue} />
    </AuthProvider>
  );
}
