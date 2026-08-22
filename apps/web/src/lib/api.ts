import type {
  Confidence,
  FactEvidence,
  Image,
  NearbyPlace,
  Source,
  SseRoutingPayload,
  Video,
} from "@rag-ragre/contracts";
import { API_QUERY_ENDPOINT, API_SSE_EVENTS } from "@rag-ragre/contracts";

/** Metadata delivered on the `done` SSE event (besides the streamed answer). */
export interface DoneMeta {
  trace_id: string;
  latency_ms: number;
  confidence?: Confidence;
  requires_review?: boolean;
}

/** Callbacks for each event type in the POST /api/query SSE stream. */
export interface QueryStreamHandlers {
  /** Story 4.5 — routing metadata emitted before the answer legs start. */
  onRouting?: (payload: SseRoutingPayload) => void;
  onSources?: (sources: Source[]) => void;
  onPlaces?: (places: NearbyPlace[]) => void;
  onFacts?: (facts: FactEvidence[]) => void;
  onImages?: (images: Image[]) => void;
  onVideos?: (videos: Video[]) => void;
  onToken?: (text: string) => void;
  onDone?: (meta: DoneMeta) => void;
  onAck?: () => void;
  onError?: (error: Error) => void;
}

interface RawSseEvent {
  event: string;
  data: unknown;
}

/** Structured `{"ok": false, "error": {"code", "message"}}` error envelope. */
export interface ApiErrorEnvelope {
  ok?: boolean;
  error?: { code?: string; message?: string };
  /** Optional project list a future backend may attach to PROJECT_SCOPE 422. */
  projects?: unknown;
}

/**
 * HTTP-level query failure carrying the backend error code so callers can
 * branch on the outcome (e.g. 422 PROJECT_SCOPE prompts the ProjectPicker).
 */
export class QueryRequestError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** Raw error envelope; may hold a project list for the picker. */
  readonly body: ApiErrorEnvelope | null;

  constructor(status: number, message: string, code: string | null, body: ApiErrorEnvelope | null = null) {
    super(message);
    this.name = "QueryRequestError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

/**
 * Streams a chat query through POST /api/query (SSE) and fans out events to
 * the provided handlers. Supports standard `event:`/`data:` framing plus a
 * `events:` batch line (JSON array of events).
 *
 * Story 10.1-FE: every query carries the persistent device id and the chosen
 * project key. project_key may be an empty string while no project is picked,
 * which the backend's default rule maps to the PROJECT_SCOPE 422.
 *
 * `signal` lets the caller cancel an in-flight stream (e.g. a project switch,
 * review M5); an aborted stream resolves silently instead of surfacing
 * onError, because cancellation is intentional, not a failure.
 */
export async function streamQuery(
  req: {
    query: string;
    session_id?: string;
    device_id?: string;
    project_key?: string;
    as_of?: string;
    history?: { role: "user" | "assistant"; content: string }[];
    /** Aborts the fetch + SSE read loop; no error is reported when set. */
    signal?: AbortSignal;
  },
  handlers: QueryStreamHandlers
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(API_QUERY_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(req),
      signal: req.signal,
    });
  } catch (cause) {
    if (req.signal?.aborted) return;
    const err = new Error("Không kết nối được máy chủ. Vui lòng thử lại.", { cause });
    handlers.onError?.(err);
    return;
  }

  if (!response.ok) {
    // The backend answers 422 PROJECT_SCOPE when more than one project is
    // active and none was chosen; surface the code so the chat can offer the
    // picker instead of a dead-end error toast.
    const body = await readErrorBody(response);
    const err = new QueryRequestError(
      response.status,
      body?.error?.message ?? `Máy chủ trả lỗi ${response.status}. Vui lòng thử lại sau.`,
      body?.error?.code ?? null,
      body
    );
    handlers.onError?.(err);
    return;
  }

  if (!response.body) {
    const err = new Error("Trình duyệt không hỗ trợ streaming phản hồi.");
    handlers.onError?.(err);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        dispatchChunk(chunk, handlers);
        boundary = buffer.indexOf("\n\n");
      }
    }
    // Flush the trailing frame that has no ending blank line.
    if (buffer.trim().length) {
      dispatchChunk(buffer, handlers);
    }
  } catch (cause) {
    if (req.signal?.aborted) return;
    const err = new Error("Lỗi khi đọc luồng phản hồi.", { cause });
    handlers.onError?.(err);
  } finally {
    reader.releaseLock();
  }
}

/** Parses one SSE chunk (event:, data:, events:) and dispatches events. */
function dispatchChunk(chunk: string, handlers: QueryStreamHandlers): void {
  let currentEvent = "";
  const dispatch: RawSseEvent[] = [];

  for (const line of chunk.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith("event:")) {
      currentEvent = trimmed.slice(6).trim();
    } else if (trimmed.startsWith("events:")) {
      // Batch line: data is a JSON array of {event, data}.
      const raw = trimmed.slice(7).trim();
      const parsed = tryJson(raw);
      if (Array.isArray(parsed)) {
        for (const item of parsed) {
          dispatch.push({
            event: String(item?.event ?? currentEvent ?? ""),
            data: item?.data,
          });
        }
        currentEvent = "";
      }
    } else if (trimmed.startsWith("data:")) {
      const raw = trimmed.slice(5).trim();
      const parsed = tryJson(raw);
      if (parsed && typeof parsed === "object" && "event" in parsed) {
        // JSON already carries its own event name.
        const obj = parsed as Record<string, unknown>;
        dispatch.push({
          event: String(obj.event),
          data: obj.data,
        });
      } else {
        dispatch.push({
          event: currentEvent || API_SSE_EVENTS.TOKEN,
          data: parsed ?? raw,
        });
      }
      currentEvent = "";
    }
  }

  for (const evt of dispatch) {
    handleEvent(evt, handlers);
  }
}

function handleEvent(evt: RawSseEvent, handlers: QueryStreamHandlers): void {
  switch (evt.event) {
    case API_SSE_EVENTS.ACK:
      handlers.onAck?.();
      break;
    case API_SSE_EVENTS.ROUTING:
      if (evt.data && typeof evt.data === "object") {
        handlers.onRouting?.(evt.data as SseRoutingPayload);
      }
      break;
    case API_SSE_EVENTS.SOURCES:
      handlers.onSources?.(asArray<Source>(evt.data));
      break;
    case API_SSE_EVENTS.PLACES:
      // Backend emits an object `{"places": [...]}`, not a bare array.
      handlers.onPlaces?.((evt.data as { places?: NearbyPlace[] } | null)?.places ?? asArray<NearbyPlace>(evt.data));
      break;
    case API_SSE_EVENTS.FACTS:
      handlers.onFacts?.(asArray<FactEvidence>(evt.data));
      break;
    case API_SSE_EVENTS.IMAGES:
      // Backend emits an object `{"images": [...]}`, not a bare array.
      handlers.onImages?.((evt.data as { images?: Image[] } | null)?.images ?? asArray<Image>(evt.data));
      break;
    case API_SSE_EVENTS.VIDEOS:
      // Greeting stream emits an object `{"videos": [...]}`, not a bare array.
      handlers.onVideos?.((evt.data as { videos?: Video[] } | null)?.videos ?? asArray<Video>(evt.data));
      break;
    case API_SSE_EVENTS.TOKEN:
      if (typeof evt.data === "string") {
        handlers.onToken?.(evt.data);
      } else if (evt.data && typeof evt.data === "object") {
        const text = (evt.data as { text?: string }).text;
        if (typeof text === "string") handlers.onToken?.(text);
      }
      break;
    case API_SSE_EVENTS.DONE:
      handlers.onDone?.(evt.data as DoneMeta);
      break;
    case API_SSE_EVENTS.ERROR: {
      const message =
        typeof evt.data === "string"
          ? evt.data
          : (evt.data as { message?: string } | null)?.message ??
            "Có lỗi xảy ra khi xử lý câu hỏi.";
      handlers.onError?.(new Error(message));
      break;
    }
    default:
      break;
  }
}
function asArray<T>(data: unknown): T[] {
  return Array.isArray(data) ? (data as T[]) : [];
}

function tryJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

// The first-open greeting text is static FE content (see greetingContent.ts),
// so it renders with zero network dependency. Projects without a curated
// static media bundle (Soleil and any future registry project) enrich the
// greeting progressively instead: text renders first, then project-scoped
// images/videos arrive from the backend hello endpoint and patch in.

const API_HELLO_ENDPOINT = "/api/llms-hello";

// Greeting media is a progressive enhancement (text renders first), so a
// hanging hello endpoint must not leave the patch pending indefinitely
// (review M9). 5s aligns with the lib timeout convention
// (routeDirections DEFAULT_TIMEOUT_MS).
const GREETING_MEDIA_TIMEOUT_MS = 5000;

/** Media attached to a project greeting by POST /api/llms-hello. */
export interface GreetingMediaPayload {
  images: Image[];
  videos: Video[];
}

/** Fetch project-scoped greeting media; rejects on non-2xx so callers can no-op. */
export async function fetchGreetingMedia(
  projectKey: string
): Promise<GreetingMediaPayload> {
  const response = await fetch(API_HELLO_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_key: projectKey }),
    signal: AbortSignal.timeout(GREETING_MEDIA_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new Error(`llms-hello failed: ${response.status}`);
  }
  const data = (await response.json()) as {
    images?: Image[];
    videos?: Video[];
  };
  return { images: data.images ?? [], videos: data.videos ?? [] };
}

/* ------------------------------------------------------------------ */
/* Lead submission (Story 5.7) - POST /api/lead.                      */
/* ------------------------------------------------------------------ */

const API_LEAD_ENDPOINT = "/api/lead";

/** Request body of `POST /api/lead` (snake_case mirrors the FastAPI model). */
export interface LeadPayload {
  /** Project the lead belongs to (backend requires it, story 10.1/G1). */
  project_key: string;
  session_id?: string;
  /** Anonymous persistent device id (D7), sent alongside the lead. */
  device_id?: string;
  name?: string;
  phone: string;
  consent: boolean;
  note?: string;
  budget_vnd?: number;
}

/** Successful `POST /api/lead` response (HTTP 201). */
export interface LeadSubmitResult {
  lead_id: number;
  will_call_within_minutes: number;
}

export type LeadSubmitErrorKind = "duplicate" | "validation" | "network";

/** Typed submit failure so LeadForm can map HTTP outcomes onto UX states. */
export class LeadSubmitError extends Error {
  readonly kind: LeadSubmitErrorKind;
  readonly status: number | null;

  constructor(kind: LeadSubmitErrorKind, message: string, status: number | null = null) {
    super(message);
    this.name = "LeadSubmitError";
    this.kind = kind;
    this.status = status;
  }
}

/**
 * Submits a customer lead. Resolves on 201; throws LeadSubmitError classified
 * as duplicate (409), validation (other 4xx, carrying the backend detail when
 * it is a plain string), or network (fetch failure / 5xx).
 */
export async function submitLead(payload: LeadPayload): Promise<LeadSubmitResult> {
  let response: Response;
  try {
    response = await fetch(API_LEAD_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new LeadSubmitError("network", "Không kết nối được máy chủ. Vui lòng thử lại.");
  }

  if (response.ok) {
    return (await response.json()) as LeadSubmitResult;
  }

  if (response.status === 409) {
    throw new LeadSubmitError(
      "duplicate",
      "Số này đã đăng ký, chuyên viên sẽ gọi sớm nhất.",
      409
    );
  }

  if (response.status >= 400 && response.status < 500) {
    const detail = await readErrorDetail(response);
    throw new LeadSubmitError(
      "validation",
      detail ?? "Thông tin chưa hợp lệ. Vui lòng kiểm tra lại.",
      response.status
    );
  }

  throw new LeadSubmitError("network", "Máy chủ gặp sự cố. Vui lòng thử lại sau.", response.status);
}

/** Best-effort extraction of a FastAPI `detail` string from an error body. */
export async function readErrorDetail(response: Response): Promise<string | null> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    return typeof data.detail === "string" ? data.detail : null;
  } catch {
    return null;
  }
}

/** Parses the backend error envelope; returns null when the body is not JSON. */
async function readErrorBody(response: Response): Promise<ApiErrorEnvelope | null> {
  try {
    return (await response.json()) as ApiErrorEnvelope;
  } catch {
    return null;
  }
}
