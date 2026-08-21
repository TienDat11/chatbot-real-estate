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

/**
 * Streams a chat query through POST /api/query (SSE) and fans out events to
 * the provided handlers. Supports standard `event:`/`data:` framing plus a
 * `events:` batch line (JSON array of events).
 */
export async function streamQuery(
  req: {
    query: string;
    session_id?: string;
    as_of?: string;
    history?: { role: "user" | "assistant"; content: string }[];
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
    });
  } catch (cause) {
    const err = new Error("Không kết nối được máy chủ. Vui lòng thử lại.", { cause });
    handlers.onError?.(err);
    return;
  }

  if (!response.ok) {
    const err = new Error(
      `Máy chủ trả lỗi ${response.status}. Vui lòng thử lại sau.`
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

// The first-open greeting is now static FE content (see greetingContent.ts), so
// there is no /llms-hello fetch path left in the client. Streaming query answers
// remain the only interactive API the chat talks to.

/* ------------------------------------------------------------------ */
/* Lead submission (Story 5.7) - POST /api/lead.                      */
/* ------------------------------------------------------------------ */

const API_LEAD_ENDPOINT = "/api/lead";

/** Request body of `POST /api/lead` (snake_case mirrors the FastAPI model). */
export interface LeadPayload {
  session_id?: string;
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
