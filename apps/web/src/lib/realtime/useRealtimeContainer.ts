"use client";

/**
 * useRealtimeContainer — reads the realtime composition root from context.
 *
 * Follows the AuthProvider useAuth() pattern: throws when used outside a
 * RealtimeProvider so misconfiguration is loud at runtime.
 */
import { useContext } from "react";
import { RealtimeContainerContext } from "./RealtimeProvider";

export function useRealtimeContainer() {
  const context = useContext(RealtimeContainerContext);
  if (!context) {
    throw new Error("useRealtimeContainer must be used within a RealtimeProvider");
  }
  return context;
}
