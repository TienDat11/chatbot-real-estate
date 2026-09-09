"use client";

import { ChatPage } from "@/components/ChatPage";

export interface SalesChatShellProps {
  /** Project key from the routed URL segment, when the route owns the scope. */
  routeProjectKey?: string;
  /** Persisted session to hydrate from a deep link. */
  sessionId?: string;
}

/** Shared sales chat canvas. The parent sales layout owns all chrome. */
export function SalesChatShell({ routeProjectKey, sessionId }: SalesChatShellProps = {}) {
  return (
    <ChatPage
      mode="sales"
      audience="sales"
      shellOwned
      routeProjectKey={routeProjectKey}
      sessionId={sessionId}
    />
  );
}
