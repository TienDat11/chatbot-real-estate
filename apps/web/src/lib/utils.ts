import { CONFIDENCE_LABELS } from "@rag-ragre/contracts";
import type { Confidence } from "@rag-ragre/contracts";

/**
 * Joins class names, dropping falsy values.
 * Usage: cn("base", isActive && "active", styles?.extra)
 */
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

/** Short Vietnamese label for a 3-tier confidence value. */
export function formatConfidence(confidence: Confidence): string {
  return CONFIDENCE_LABELS[confidence];
}

/**
 * Formats a latency in milliseconds as a compact string:
 * "850 ms" under a second, "2.5 s" above.
 */
export function formatLatency(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Formats an ISO date string as dd/mm/yyyy (returns the input when invalid). */
export function toDisplayDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const day = String(date.getDate()).padStart(2, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  return `${day}/${month}/${date.getFullYear()}`;
}
