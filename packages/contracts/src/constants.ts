import type { Confidence, SseEventName } from "./index";

/**
 * Default local FastAPI proxy target used when NEXT_PUBLIC_API_PROXY_TARGET
 * is not set (see apps/web/next.config.ts).
 */
export const DEFAULT_API_PROXY_TARGET = "http://localhost:8000";

/** Relative endpoint proxied to FastAPI for the streaming chat query. */
export const API_QUERY_ENDPOINT = "/api/query";

/** Canonical SSE event names emitted by the FastAPI /api/query stream. */
export const API_SSE_EVENTS = {
  PLACES: "places",
  SOURCES: "sources",
  FACTS: "facts",
  TOKEN: "token",
  DONE: "done",
  ERROR: "error",
} as const satisfies Record<string, SseEventName>;

/** Every SSE event name in the stream, for iteration and validation. */
export const SSE_EVENT_NAMES: readonly SseEventName[] = [
  API_SSE_EVENTS.PLACES,
  API_SSE_EVENTS.SOURCES,
  API_SSE_EVENTS.FACTS,
  API_SSE_EVENTS.TOKEN,
  API_SSE_EVENTS.DONE,
  API_SSE_EVENTS.ERROR,
];

/** Short Vietnamese labels for the 3-tier confidence score. */
export const CONFIDENCE_LABELS: Record<Confidence, string> = {
  HIGH: "Cao",
  MEDIUM: "Trung bình",
  LOW: "Thấp",
};
