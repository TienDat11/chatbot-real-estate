import type { ReactElement } from "react";
import { redirect } from "next/navigation";
import { SalesChatShell } from "@/features/sales/SalesChatShell";
import {
  salesChatProjectUrl,
  type SalesChatRouteContext,
} from "@/features/sales/salesChatRoute";
import { resolveSessionParam } from "@/lib/canonicalProjectUrl";

/**
 * Sales chat route adapter. The bare legacy /sales/chat mount renders the
 * shared canvas synchronously and leaves the project choice to the picker; a
 * project-scoped route context (as delivered under
 * /sales/chat/project/[projectKey]) normalizes onto the canonical URL.
 * redirect() throws inside Next before the element is used, so the returned
 * canvas below the redirect only materializes where redirect is mocked.
 */
export default function SalesChatPage(
  routeContext: SalesChatRouteContext = {},
): ReactElement | Promise<ReactElement> {
  const { params, searchParams } = routeContext;
  if (params !== undefined || searchParams !== undefined) {
    return mountRoutedSalesChat(params, searchParams);
  }
  return <SalesChatShell />;
}

async function mountRoutedSalesChat(
  params?: SalesChatRouteContext["params"],
  searchParams?: SalesChatRouteContext["searchParams"],
): Promise<ReactElement> {
  const projectKey = params ? (await params).projectKey : undefined;
  const sessionId = searchParams ? resolveSessionParam(await searchParams) : undefined;
  if (projectKey) {
    redirect(salesChatProjectUrl(projectKey, sessionId));
  }
  return <SalesChatShell routeProjectKey={projectKey} sessionId={sessionId} />;
}
