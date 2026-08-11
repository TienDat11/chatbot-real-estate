import type { Confidence, FactEvidence, Source } from "@rag-ragre/contracts";
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
  onSources?: (sources: Source[]) => void;
  onFacts?: (facts: FactEvidence[]) => void;
  onToken?: (text: string) => void;
  onDone?: (meta: DoneMeta) => void;
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
    case API_SSE_EVENTS.SOURCES:
      handlers.onSources?.(asArray<Source>(evt.data));
      break;
    case API_SSE_EVENTS.FACTS:
      handlers.onFacts?.(asArray<FactEvidence>(evt.data));
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
