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

/**
 * Wall-clock cap for one POST /api/query SSE stream. A cold LightRAG first
 * query can stay silent for 40s+ before the first token; 180s keeps that
 * alive while still failing a truly stalled stream fast enough for the
 * interrupted-stream retry UX.
 */
export const QUERY_STREAM_TIMEOUT_MS = 180_000;

/** Metadata delivered on the `done` SSE event (besides the streamed answer). */
export interface DoneMeta {
  trace_id: string;
  latency_ms: number;
  confidence?: Confidence;
  requires_review?: boolean;
  /**
   * Secure wave §5.3: the final sanitized answer (authoritative) — callers
   * replace the streamed message body with it.
   */
  answer?: string;
  /** Post-consumption quota snapshot (authoritative, §5.1 shape). */
  quota?: unknown;
  /** Newly minted anon token when the caller had none/invalid (§5.3). */
  anon_token?: string;
  /**
   * Cross-project guardrail (optional, ships incrementally): when the question
   * targets another active project the backend suggests the destination here.
   * Kept `unknown` because validation lives in normalizeProjectRedirect —
   * one file owns the shape so backend drift cannot crash the chat.
   */
  project_redirect?: unknown;
}

/** Payload of the SSE `ack` event since the secure wave (§5.3). */
export interface AckMeta {
  /** Pre-consumption quota snapshot (§5.1 shape). */
  quota?: unknown;
  /** Newly minted anon token when the caller sent none/invalid. */
  anon_token?: string;
}

/**
 * SSE `error` frame carrying a structured backend envelope (e.g.
 * ANONYMOUS_QUOTA_EXCEEDED, §5.3); plain-string errors stay plain Errors so
 * existing consumers are unaffected.
 */
export class QueryStreamError extends Error {
  /** Raw frame data: `{code, message?, quota?, lead_cta?}` per spec §5.2/§5.3. */
  readonly data: unknown;

  constructor(message: string, data: unknown) {
    super(message);
    this.name = "QueryStreamError";
    this.data = data;
  }
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
  /** Secure wave §5.3: ack may carry the quota snapshot + a minted token. */
  onAck?: (meta?: AckMeta) => void;
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
 * Injectable bearer-token source for POST /api/query: when a provider is
 * installed and resolves a token, every query ships
 * `Authorization: Bearer <token>` so the backend treats the session as
 * authenticated (sales/admin get quota cap null) instead of burning the
 * anonymous allowance. Registration is a UI concern — ChatPage installs the
 * Firebase ID-token reader on mount and clears it on unmount, so other
 * streamQuery callers (e.g. TrainWorkspace) are unaffected unless they opt in.
 */
export type QueryAuthTokenProvider = () => Promise<string | null> | string | null;

let queryAuthTokenProvider: QueryAuthTokenProvider | null = null;

/** Installs (or, with null, removes) the per-request /query token provider. */
export function setQueryAuthTokenProvider(provider: QueryAuthTokenProvider | null): void {
  queryAuthTokenProvider = provider;
}

/**
 * Explicit per-call bearer credential override. The training gate mints and
 * validates ONE Firebase ID token per send and passes it here so the actual
 * fetch uses exactly that credential — the per-request provider seam is
 * skipped, closing the sign-out race where a second provider resolve between
 * "gate" and "request" returns null and a bearer-less /query leaves the
 * browser. A null/empty override blocks the request outright instead of
 * degrading to no header.
 */
export interface QueryAuthOverride {
  bearerToken: string | null;
}

/**
 * Resolves the Authorization headers for ONE query; never rejects — an absent
 * provider, an empty/null token or a throwing provider all degrade to "no
 * header", which keeps the historical anonymous flow byte-identical.
 */
async function resolveQueryAuthHeaders(): Promise<Record<string, string>> {
  if (!queryAuthTokenProvider) return {};
  try {
    const token = await queryAuthTokenProvider();
    return typeof token === "string" && token.length > 0
      ? { Authorization: `Bearer ${token}` }
      : {};
  } catch {
    return {};
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
 *
 * Bearer seam: when a token provider is installed (setQueryAuthTokenProvider)
 * each request carries a fresh `Authorization: Bearer <token>` header; absent
 * provider or null token sends no header at all (anonymous flow unchanged).
 * `authOverride` instead pins one caller-minted credential to THIS request
 * (single mint, provider skipped) or blocks the request when null/empty.
 */
export async function streamQuery(
  req: {
    query: string;
    session_id?: string;
    device_id?: string;
    project_key?: string;
    as_of?: string;
    history?: { role: "user" | "assistant"; content: string }[];
    /**
     * Story 11.3 / ISSUE-5: /train sends "training" so the backend answers
     * from the _training namespace with the coaching prompt (no sales persona,
     * no CTA). Typed contract mirrors the backend
     * QueryRequest.answer_mode: Literal["normal","training"]; backends
     * predating it ignore the field harmlessly.
     */
    answer_mode?: "normal" | "training";
    context?: { project_key: string };
    /**
     * Secure wave §5.6: server-minted signed anonymous identity token. Sent
     * on every query so the backend can key quota to a durable identity; the
     * response/ack self-heals by returning a fresh token when absent/invalid.
     */
    anon_token?: string;
    /** Aborts the fetch + SSE read loop; no error is reported when set. */
    signal?: AbortSignal;
  },
  handlers: QueryStreamHandlers,
  // Single-mint override (security wave review): see QueryAuthOverride.
  authOverride?: QueryAuthOverride
): Promise<void> {
  // Bearer seam: an explicit override pins ONE caller-minted credential to
  // this request (single mint — the provider seam is never consulted a second
  // time, so a sign-out between gate and fetch cannot strip the header);
  // null/empty blocks the fetch outright so no bearer-less /query can leave.
  // Without an override the per-request provider resolves fresh per call;
  // absent provider/null token degrades to no extra headers (anonymous flow).
  let authHeaders: Record<string, string>;
  if (authOverride !== undefined) {
    if (authOverride.bearerToken === null || authOverride.bearerToken.length === 0) {
      handlers.onError?.(new Error("Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại."));
      return;
    }
    authHeaders = { Authorization: `Bearer ${authOverride.bearerToken}` };
  } else {
    authHeaders = await resolveQueryAuthHeaders();
  }

  // Hard cap on the whole stream (see QUERY_STREAM_TIMEOUT_MS), combined with
  // the caller's own signal. AbortSignal.any keeps the two independent: an
  // intentional caller abort stays silent (checked below via req.signal), while
  // a timeout abort still surfaces onError so the interrupted-stream UX fires.
  const streamSignal = req.signal
    ? AbortSignal.any([req.signal, AbortSignal.timeout(QUERY_STREAM_TIMEOUT_MS)])
    : AbortSignal.timeout(QUERY_STREAM_TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(API_QUERY_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(req.device_id ? { "X-Device-Id": req.device_id } : {}),
        ...authHeaders,
      },
      body: JSON.stringify(req),
      signal: streamSignal,
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
      handlers.onAck?.(
        evt.data && typeof evt.data === "object" ? (evt.data as AckMeta) : undefined
      );
      break;
    case API_SSE_EVENTS.ROUTING:
      if (evt.data && typeof evt.data === "object") {
        handlers.onRouting?.(evt.data as SseRoutingPayload);
      }
      break;
    case API_SSE_EVENTS.SOURCES:
      handlers.onSources?.(
        (evt.data as { sources?: Source[] } | null)?.sources ?? asArray<Source>(evt.data)
      );
      break;
    case API_SSE_EVENTS.PLACES:
      // Backend emits an object `{"places": [...]}`, not a bare array.
      handlers.onPlaces?.((evt.data as { places?: NearbyPlace[] } | null)?.places ?? asArray<NearbyPlace>(evt.data));
      break;
    case API_SSE_EVENTS.FACTS:
      handlers.onFacts?.(
        (evt.data as { facts?: FactEvidence[] } | null)?.facts ?? asArray<FactEvidence>(evt.data)
      );
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
      // Structured frames (e.g. ANONYMOUS_QUOTA_EXCEEDED, §5.3) keep their
      // envelope on a typed error so callers can branch without re-parsing;
      // plain-string frames stay the historical plain Error.
      if (evt.data && typeof evt.data === "object") {
        const rec = evt.data as { message?: unknown };
        const message =
          typeof rec.message === "string" && rec.message.length > 0
            ? rec.message
            : "Có lỗi xảy ra khi xử lý câu hỏi.";
        handlers.onError?.(new QueryStreamError(message, evt.data));
        break;
      }
      const message =
        typeof evt.data === "string"
          ? evt.data
          : "Có lỗi xảy ra khi xử lý câu hỏi.";
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
// (review M9). The backend LLM (POST /api/llms-hello) was measured returning
// 5.0-5.5s in QA, so 5s caused deterministic aborts (net::ERR_ABORTED) and
// a flaky gallery; 15s gives that latency a ceiling plus margin while still
// bounding a hung endpoint.
const GREETING_MEDIA_TIMEOUT_MS = 15000;

/** Media attached to a project greeting by POST /api/llms-hello. */
export interface GreetingPayload {
  greeting?: string;
  suggestions?: string[];
  images?: Image[];
  videos?: Video[];
}

export interface GreetingMediaPayload {
  images: Image[];
  videos: Video[];
}

/** Fetches the project/audience-scoped first assistant greeting. */
export async function fetchGreeting(
  projectKey: string,
  options: { deviceId?: string; audience?: "customer" | "sales" } = {},
): Promise<GreetingPayload> {
  const response = await fetch(API_HELLO_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(options.deviceId ? { "X-Device-Id": options.deviceId } : {}),
    },
    body: JSON.stringify({
      project_key: projectKey,
      audience: options.audience ?? "customer",
    }),
    signal: AbortSignal.timeout(GREETING_MEDIA_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`llms-hello failed: ${response.status}`);
  return (await response.json()) as GreetingPayload;
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

export interface ChatSessionSummary {
  session_id: string;
  project_key: string;
  title: string;
  message_count: number;
  handed_off: boolean;
  last_active_at: string;
}

export interface ChatSessionMessage {
  role: "user" | "assistant";
  content: string;
  meta?: Record<string, unknown> | null;
  created_at: string;
}

export interface TrainingSessionSummary {
  id: string;
  title: string | null;
  context: { project_key: string | null };
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface TrainingSessionDetail extends TrainingSessionSummary {
  answer_mode: "training";
  messages: ChatSessionMessage[];
}

export async function fetchTrainingSessions(projectKey: string): Promise<TrainingSessionSummary[]> {
  // The training history endpoints live on the sales router, whose prefix is
  // /api/sales — a bare /api/training path 404s against the backend.
  const response = await fetch(`/api/sales/training/sessions?project_key=${encodeURIComponent(projectKey)}`, {
    headers: await resolveQueryAuthHeaders(),
  });
  if (!response.ok) throw new Error(`training sessions failed: ${response.status}`);
  const payload = (await response.json()) as { items?: TrainingSessionSummary[] };
  return payload.items ?? [];
}

export async function fetchTrainingSession(sessionId: string): Promise<TrainingSessionDetail> {
  const response = await fetch(`/api/sales/training/sessions/${encodeURIComponent(sessionId)}`, {
    headers: await resolveQueryAuthHeaders(),
  });
  if (!response.ok) throw new Error(`training session failed: ${response.status}`);
  return (await response.json()) as TrainingSessionDetail;
}

/**
 * Identity headers shared by every history/session endpoint (secure wave):
 * the backend answers 401 unless BOTH the persistent device id and the signed
 * anon token are present, then scopes rows by their combination — so a
 * mismatched identity is an intentional ownership rejection the callers
 * surface as a recoverable error, never retried with faked credentials.
 */
async function chatSessionHeaders(deviceId: string, anonToken: string | null): Promise<Record<string, string>> {
  return {
    Accept: "application/json",
    "X-Device-Id": deviceId,
    ...(anonToken ? { "X-Anon-Token": anonToken } : {}),
    ...(await resolveQueryAuthHeaders()),
  };
}

/**
 * Loads persisted sessions for the current device and project. Requires the
 * signed anon identity: GET /api/sessions 401s without `X-Device-Id` +
 * `X-Anon-Token`, and a null `anonToken` therefore surfaces that rejection.
 */
export async function fetchChatSessions(
  deviceId: string,
  projectKey: string,
  anonToken: string | null
): Promise<ChatSessionSummary[]> {
  const response = await fetch(`/api/sessions?device_id=${encodeURIComponent(deviceId)}&project_key=${encodeURIComponent(projectKey)}`, {
    headers: await chatSessionHeaders(deviceId, anonToken),
  });
  if (!response.ok) throw new Error(`sessions failed: ${response.status}`);
  const payload = (await response.json()) as { sessions?: ChatSessionSummary[] } | ChatSessionSummary[];
  return Array.isArray(payload) ? payload : payload.sessions ?? [];
}

/**
 * Loads a persisted transcript; callers may render it read-only. `project_key`
 * is a REQUIRED backend query parameter (its absence is a 422) and, together
 * with the two identity headers, scopes the lookup so another visitor's
 * session id resolves to an intentional 404 instead of leaking a transcript.
 */
export async function fetchChatSessionMessages(
  deviceId: string,
  sessionId: string,
  projectKey: string,
  anonToken: string | null
): Promise<ChatSessionMessage[]> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages?project_key=${encodeURIComponent(projectKey)}`, {
    headers: await chatSessionHeaders(deviceId, anonToken),
  });
  if (!response.ok) throw new Error(`session messages failed: ${response.status}`);
  const payload = (await response.json()) as { messages?: ChatSessionMessage[] } | ChatSessionMessage[];
  return Array.isArray(payload) ? payload : payload.messages ?? [];
}

const API_LEAD_ENDPOINT = "/api/lead";

/** Request body of `POST /api/lead` (snake_case mirrors the FastAPI model). */
export interface LeadPayload {
  /** Project the lead belongs to (backend requires it, story 10.1/G1). */
  project_key: string;
  session_id?: string;
  /** Anonymous persistent device id (D7), sent alongside the lead. */
  device_id?: string;
  /**
   * Server-minted signed anon identity (secure wave §5.5/§6) so POST
   * /api/lead can grant the one-time bonus to the chatting identity.
   */
  anon_token?: string;
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
  /**
   * Secure wave §5.5: bonus turns granted with this lead (0 when the identity
   * already received its one-time grant, rule R3).
   */
  quota_bonus_granted?: number;
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
      headers: {
        "Content-Type": "application/json",
        // Guard sync: POST /api/lead matches session.device_id against this
        // header (or the body) so the handed-off badge and CRM link bind to the
        // same identity that chatted. Same device_id already rides in the body.
        ...(payload.device_id ? { "X-Device-Id": payload.device_id } : {}),
      },
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
