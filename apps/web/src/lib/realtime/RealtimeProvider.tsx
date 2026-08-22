"use client";

/**
 * RealtimeProvider — React binding that exposes the realtime PORTS.
 *
 * Follows the AuthProvider pattern (src/lib/AuthProvider.tsx): a client
 * context whose value is the wired composition root. Components receive the
 * container (ports + use-case service) and NEVER raw firestore objects, so the
 * React layer stays transport-agnostic.
 *
 * MOUNTING NOTE (wave 2): do NOT add this provider to app/layout.tsx yet — the
 * wave-2 story owns mounting (see lib/realtime/README.md). Until then, wrap
 * only the components that consume useLeadRealtimeStream.
 */
import { createContext, type ReactNode } from "react";
import {
  REALTIME_CONTAINER,
  type RealtimeContainer,
} from "@/infrastructure/realtimeContainer";

export const RealtimeContainerContext = createContext<RealtimeContainer | null>(
  null
);

export function RealtimeProvider({ children }: { children: ReactNode }) {
  return (
    <RealtimeContainerContext.Provider value={REALTIME_CONTAINER}>
      {children}
    </RealtimeContainerContext.Provider>
  );
}
