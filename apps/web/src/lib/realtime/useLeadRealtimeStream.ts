"use client";

/**
 * useLeadRealtimeStream — demo consumer hook for the per-project lead stream.
 *
 * Subscribes through the application service (which only knows the
 * LeadRepositoryPort), so this hook works unchanged after a transport swap.
 * Returns the live lead list plus the coarse connection state and the latest
 * channel error. The returned handle is unsubscribed on unmount.
 */
import { useEffect, useState } from "react";
import type { Lead } from "@/domain/crm/lead";
import type {
  RealtimeChannelError,
  RealtimeConnectionState,
} from "@/domain/realtime/realtimeSubscription";
import { useRealtimeContainer } from "./useRealtimeContainer";

export interface LeadRealtimeStreamState {
  leads: Lead[];
  connectionState: RealtimeConnectionState;
  error: RealtimeChannelError | null;
}

export function useLeadRealtimeStream(
  projectKey: string
): LeadRealtimeStreamState {
  const { leadRealtimeService } = useRealtimeContainer();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [connectionState, setConnectionState] =
    useState<RealtimeConnectionState>("connecting");
  const [error, setError] = useState<RealtimeChannelError | null>(null);

  useEffect(() => {
    // Reset the transient stream state at the START of every (re)subscription:
    // switching projectKey must not keep the previous project's error (or lead
    // list) visible while the new subscription is still connecting.
    setLeads([]);
    setError(null);
    setConnectionState("connecting");

    const handle = leadRealtimeService.streamLeadsByProject(
      { projectKey },
      {
        onLeadsChanged: setLeads,
        onConnectionStateChanged: setConnectionState,
        onError: setError,
      }
    );
    return () => handle.unsubscribe();
  }, [leadRealtimeService, projectKey]);

  return { leads, connectionState, error };
}
