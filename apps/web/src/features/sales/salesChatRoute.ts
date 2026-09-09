/** Next route context shared by the sales chat route adapters. */
export type SalesChatRouteContext = {
  params?: Promise<{ projectKey?: string }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

/**
 * Sales chat entry URL without a project scope (the picker surface), with the
 * session preserved when one was carried in.
 */
export function salesChatUrl(sessionId?: string | null): string {
  if (!sessionId) return "/sales/chat";
  return `/sales/chat?sessionId=${encodeURIComponent(sessionId)}`;
}

export { SALES_CHAT_PROJECT_PREFIX, salesChatProjectUrl } from "@/lib/canonicalProjectUrl";
