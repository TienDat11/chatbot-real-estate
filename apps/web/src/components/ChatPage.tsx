"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { App as AntApp, Button, Select, Typography } from "antd";
import {
  CrownOutlined,
  DownloadOutlined,
  EnvironmentOutlined,
  LockFilled,
  PhoneOutlined,
  ReadOutlined,
  SafetyCertificateOutlined,
  SwapOutlined,
  ThunderboltOutlined,
  TeamOutlined,
  MessageOutlined,
} from "@ant-design/icons";
import { Disclaimer } from "@rag-ragre/ui";
import type { NearbyPlace } from "@rag-ragre/contracts";
import { ProThemeProvider } from "@/components/ProThemeProvider";
import {
  QueryRequestError,
  QueryStreamError,
  fetchGreeting,
  fetchChatSessionMessages,
  fetchTrainingSession,
  isInferRetryableStreamError,
  setQueryAuthTokenProvider,
  streamQuery,
} from "@/lib/api";
import type { DoneMeta } from "@/lib/api";
import { firebaseQueryAuthToken } from "@/features/auth/queryAuthToken";
import { ASK_EVENT } from "@/lib/constants";
import type { ChatMessage } from "@/components/MessageBubble";
import { AccessibilityControls } from "@/components/AccessibilityControls";

import { AccountControls } from "@/components/AccountControls";
import { warmPrefetchCache } from "@/lib/prefetch";
import { MessageList } from "./MessageList";
import { ChatHistoryDrawer } from "./ChatHistoryDrawer";
import { Composer } from "./Composer";
import { MapPanel, DEFAULT_PROJECT } from "./MapPanel";
import { LeadForm, LEAD_ID_STORAGE_KEY } from "./LeadForm";
import { STATIC_PLACES } from "@/lib/places";
import { C, HEADER, RADIUS, SHADOW } from "@/lib/tokens";
import {
  getDeviceId,
  getAnonToken,
  getSessionIdForScope,
  sessionKeyForScope,
  persistAnonToken,
  storeProjectKey,
} from "@/features/chat/identity";
import {
  QUOTA_EXCEEDED_CODE,
  applyLeadBonus,
  isQuotaExhausted,
  normalizeQuota,
  parseQuotaErrorEnvelope,
  quotaBadgeLabel,
  shouldForceLeadForm,
  shouldShowLeadBonus,
} from "@/features/chat/quota";
import type { QuotaExceededInfo, QuotaState } from "@/features/chat/quota";
import {
  loadActiveProjects,
  FALLBACK_ACTIVE_PROJECTS,
  sortActiveProjects,
  projectDisplayName,
  projectDisplayLocation,
  projectShortName,
} from "@/features/chat/activeProjects";
import type { ActiveProject } from "@/features/chat/activeProjects";
import { ProjectPicker } from "@/features/chat/ProjectPicker";
import { fetchProjectCatalog } from "@/lib/projectCatalog";
import { decideRoutedProject } from "@/lib/projectRoute";
import {
  applySessionParam,
  projectChatUrl,
  SALES_CHAT_PROJECT_PREFIX,
  type ChatSurface,
} from "@/lib/canonicalProjectUrl";
import { normalizeProjectRedirect } from "@/lib/projectRedirect";
import { routesForRole } from "@/lib/roleRoutes";
import { useOptionalAuth } from "@/lib/AuthProvider";

const HELLO_SHOWN_KEY = "ragre.hello_shown";
const MAX_TURNS = 4;
// Internal bucket key for training mode. It is NEVER sent as project_key: the
// backend picks the _training namespace from answer_mode alone, so this key
// only scopes the local per-project records (messages/quota) and stays hidden
// from every customer-facing surface (picker, chip, history).
const TRAINING_SCOPE_KEY = "__training__";
// First paint of the training workspace: a stable intro instead of a server
// greeting (training must not touch project-scoped greeting endpoints).
const TRAINING_INTRO_MESSAGE: ChatMessage = {
  id: "train-intro",
  role: "assistant",
  content:
    "Chào bạn - đây là chế độ đào tạo nội bộ. Hãy hỏi bất kỳ điều gì về quy trình bán hàng, chính sách dự án hoặc pháp lý bất động sản; câu trả lời luôn kèm trích dẫn tài liệu đào tạo để bạn đối chiếu nguồn.",
};
// Deep-link transcript hydration gives up after this many attempts per session
// id. The effect only re-runs when one of its dependencies changes, so the cap
// bounds the total network calls across dep churn without ever latching shut
// permanently or spinning in a loop; presentation stays the existing empty chat.
const MAX_SESSION_HYDRATION_ATTEMPTS = 2;

// Secure wave §5.6: mint endpoint for the signed anonymous identity. Called
// lazily on the first chat interaction; /query also self-heals by returning
// a fresh token, so a failure here never blocks chatting.
const ANON_TOKEN_ENDPOINT = "/api/anon/token";
const ANON_TOKEN_FETCH_TIMEOUT_MS = 3000;
// Fallback wall copy when the 429/error frame arrives without a message.
const QUOTA_WALL_FALLBACK_MESSAGE =
  "Anh/chị đã dùng hết lượt tư vấn miễn phí. Để lại số điện thoại để nhận thêm lượt tư vấn miễn phí nhé!";

// Read the map mode from the URL query string so a refresh (F5) restores the
// list view without remounting the chat (state is local). The rail tab no
// longer exists (map is always visible), so a legacy ?tab= param is ignored.
function initialMapModeFromUrl(): "map" | "list" {
  if (typeof window === "undefined") return "map";
  const params = new URLSearchParams(window.location.search);
  return params.get("mode") === "list" ? "list" : "map";
}

// Reflect the current map mode in the URL (query string only). Using
// history.replaceState avoids a Next route transition, so the ChatPage
// component stays mounted and all local state (messages, streaming) survives.
// The legacy ?tab= param is always stripped on write.
function syncModeUrl(mode: "map" | "list"): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  params.delete("tab");
  if (mode === "list") params.set("mode", "list");
  else params.delete("mode");
  const qs = params.toString();
  const nextUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
  if (window.location.href !== nextUrl) window.history.replaceState(null, "", nextUrl);
}

/**
 * Routes whose canonical ?sessionId= is shareable and therefore synced: the
 * customer project surface, the routed sales project surface, plus the bare
 * sales chat and training workspaces (deep-linking a training/customer session
 * from /sales/train or /sales/chat must round-trip like /project/* does).
 */
function isSessionUrlRoute(pathname: string): boolean {
  return (
    pathname.startsWith("/project/") ||
    pathname.startsWith(`${SALES_CHAT_PROJECT_PREFIX}/`) ||
    pathname === "/sales/chat" ||
    pathname === "/sales/train"
  );
}

/** Keeps the canonical project routes (customer + sales) shareable while preserving the mounted chat. */
export function syncSessionUrl(session: string): void {
  const onSessionUrlRoute =
    typeof window !== "undefined" && isSessionUrlRoute(window.location.pathname);
  if (!session || !onSessionUrlRoute) return;
  const params = new URLSearchParams(window.location.search);
  applySessionParam(params, session);
  const nextUrl = `${window.location.pathname}?${params.toString()}`;
  if (window.location.href !== nextUrl) window.history.replaceState(null, "", nextUrl);
}

/**
 * Removes the canonical (and legacy) session param from the current URL in
 * place, used when a deep link proves unresolvable and must not keep advertising
 * a session the canvas no longer shows. Silent by design - no toast, no nav.
 */
function clearSessionParamFromUrl(): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  if (!params.has("sessionId") && !params.has("session")) return;
  params.delete("sessionId");
  params.delete("session");
  const qs = params.toString();
  const nextUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
  if (window.location.href !== nextUrl) window.history.replaceState(null, "", nextUrl);
}

/**
 * The session fetch helpers throw a plain Error whose message ends in the HTTP
 * status (e.g. "training session failed: 404"); recover it so the training
 * hydration path can distinguish a permanent 403/404 rejection (clear + latch)
 * from a transient failure (allow a bounded retry).
 */
function httpStatusFromError(error: unknown): number | null {
  const message = error instanceof Error ? error.message : String(error);
  const match = /(\d{3})\s*$/.exec(message);
  return match ? Number(match[1]) : null;
}

/**
 * Whether a training 409 is a session-collision the self-heal should recover
 * from. Prefers the stable backend codes; when an older backend omits
 * `error.code` entirely, any training 409 is treated as a conflict (defense in
 * depth) so the collision never dead-ends the trainee. Non-training 409s are
 * never routed here (the caller gates on isTraining).
 */
function isTrainingSessionConflict(err: QueryRequestError): boolean {
  if (
    err.code === "TRAINING_SESSION_CONFLICT" ||
    err.code === "TRAINING_SESSION_CONTEXT_MISMATCH"
  ) {
    return true;
  }
  return err.code == null;
}

// R3 (FR-18): a history-selected session keeps a STABLE deep-link marker in the
// URL so a refresh/re-share reopens the same transcript deterministically.
export const HISTORY_URL_PARAM = "history";
// Only harmless presentation params survive the rewrite; everything else
// (legacy session spellings, tracking junk, stale state) is dropped.
const PROJECT_URL_SAFE_PARAMS: readonly string[] = ["mode"];

/**
 * Builds the canonical project URL for a history-selected session: canonical
 * `sessionId` + `history=1` indicator over a whitelist-preserved query string,
 * on the requested surface (customer /project/* or sales workspace route).
 * Pure (input search string -> URL path), so vitest pins the contract directly.
 */
export function buildProjectHistoryUrl(projectKey: string, sessionId: string, search: string, surface: ChatSurface = "customer"): string {
  const params = new URLSearchParams(search);
  for (const key of Array.from(params.keys())) {
    if (!PROJECT_URL_SAFE_PARAMS.includes(key)) params.delete(key);
  }
  params.set("sessionId", sessionId);
  params.set(HISTORY_URL_PARAM, "1");
  return `${projectChatUrl(surface, projectKey)}?${params.toString()}`;
}

// R5 (FR-20, G3-r4 amendment): the exhausted-quota wall must survive a reload.
// The snapshot is keyed by BOTH a non-empty signed anon token AND a non-empty
// project_key — one identity's wall on one project never leaks to another
// identity/project. The server stays authoritative: its first ack/done/429
// after reload re-asserts the real allowance; this mark only keeps the wall +
// LeadForm button visible until then. Writes with an empty token or empty
// project are skipped entirely; there is NO shared empty sentinel (a legacy
// token-only or empty row reads as no mark, so the server re-walls).
export const QUOTA_EXHAUSTED_STORAGE_KEY = "ragre.quota_exhausted";

/** Minimal Storage subset for the quota mark (mirrors identity.StorageLike). */
interface QuotaMarkStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

interface QuotaExhaustedMarkPayload {
  t: string;
  p: string;
}

function hasMarkIdentity(
  anonToken: string | null | undefined,
  projectKey: string | null | undefined
): boolean {
  return typeof anonToken === "string" && anonToken.length > 0 &&
    typeof projectKey === "string" && projectKey.length > 0;
}

function parseQuotaExhaustedMark(stored: string | null): QuotaExhaustedMarkPayload | null {
  if (!stored || stored.length === 0) return null;
  try {
    const parsed = JSON.parse(stored) as Partial<QuotaExhaustedMarkPayload>;
    if (!hasMarkIdentity(parsed.t ?? null, parsed.p ?? null)) return null;
    return { t: parsed.t as string, p: parsed.p as string };
  } catch {
    // A legacy/garbage row reads as no mark (the next server 429 re-walls).
    return null;
  }
}

/**
 * Persists the exhausted-quota mark for this anon token + project pair. An
 * empty token or empty project SKIPS the write entirely — a wall can never be
 * stored under a shared/empty sentinel that other visitors would inherit.
 * (The lead-bonus payoff lifts the persisted row via clearQuotaExhaustedMark.)
 */
export function persistQuotaExhaustedMark(storage: QuotaMarkStorage, anonToken: string, projectKey: string): void {
  if (!hasMarkIdentity(anonToken, projectKey)) return;
  const payload: QuotaExhaustedMarkPayload = { t: anonToken, p: projectKey };
  storage.setItem(QUOTA_EXHAUSTED_STORAGE_KEY, JSON.stringify(payload));
}

/**
 * True when the stored mark matches BOTH this anon token AND this project key;
 * any mismatch/missing half is discarded so no restored wall survives it.
 */
export function wasQuotaExhaustedMarked(
  storage: Pick<QuotaMarkStorage, "getItem">,
  anonToken: string | null,
  projectKey: string | null
): boolean {
  if (!hasMarkIdentity(anonToken, projectKey)) return false;
  const stored = parseQuotaExhaustedMark(storage.getItem(QUOTA_EXHAUSTED_STORAGE_KEY));
  return stored !== null && stored.t === anonToken && stored.p === projectKey;
}

/** Removes the persisted mark (lead-bonus payoff lifts the wall for good). */
export function clearQuotaExhaustedMark(storage: QuotaMarkStorage): void {
  try {
    storage.removeItem?.(QUOTA_EXHAUSTED_STORAGE_KEY);
  } catch {
    // Storage without removeItem support degrades to an unreadable row.
    storage.setItem(QUOTA_EXHAUSTED_STORAGE_KEY, "");
  }
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Builds a best-effort lead note (<= 200 chars) from the most recent answer
 * facts so the broker sees what the customer was asking about. Returns
 * undefined when no facts exist yet (the backend treats note as optional).
 */
export function buildLeadNote(messages: ChatMessage[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.role !== "assistant" || !msg.facts || msg.facts.length === 0) continue;
    const subjects = msg.facts.map((f) => f.subject).filter((s) => s.trim().length > 0);
    if (subjects.length === 0) continue;
    return `Quan tâm: ${subjects.join(", ")}`.slice(0, 200);
  }
  return undefined;
}


/**
 * Filters backend greeting suggestions so no sibling project's name leaks into
 * the empty-state prompts (a Camellia visitor must never be offered a Soleil
 * question). Pure so the "late catalogue" regression is unit-testable: callers
 * pass the catalogue that is live at RESOLUTION time (activeProjectsRef), not
 * the list captured when the greeting was fired.
 */
export function sanitizeGreetingSuggestions(
  suggestions: unknown,
  projects: ActiveProject[],
  currentProjectKey: string
): string[] {
  if (!Array.isArray(suggestions)) return [];
  const otherProjectNames = projects
    .filter((project) => project.project_key !== currentProjectKey)
    .flatMap((project) => [
      projectDisplayName(project),
      projectShortName(project),
      // The bare key ("soleil") leaks too: short suggestions often drop the
      // "The"/brand prefix but keep the key word.
      project.project_key,
    ])
    .filter((name, index, names) => name.length > 0 && names.indexOf(name) === index);
  return suggestions.filter((item): item is string =>
    typeof item === "string" &&
    item.trim().length > 0 &&
    !otherProjectNames.some((name) => item.toLocaleLowerCase().includes(name.toLocaleLowerCase())),
  );
}

/** Explicit, typed chat surface. Drives every mode branch in one place. */
export type ChatPageMode = "customer" | "sales" | "training";

export interface ChatPageProps {
  /** Project key from the routed URL segment. */
  routeProjectKey?: string;
  /** Greeting audience; sales receives the authenticated sales greeting. */
  audience?: "customer" | "sales";
  /** Optional persisted session to hydrate from a deep link. */
  sessionId?: string;
  /**
   * Explicit surface mode (never derived from the pathname): "training" runs
   * the internal sales-training chat (answer_mode=training, no project scope,
   * no selling surfaces, no quota wall); "sales" is the authenticated
   * customer-chat with staff navigation; "customer" is the default storefront.
   */
  mode?: ChatPageMode;
  /** Hide the legacy canvas header when AppShell owns the sales chrome. */
  shellOwned?: boolean;
}

/** Main chat layout: single page with chat and evidence rail side by side. */
export function ChatPage({ routeProjectKey, audience = "customer", sessionId, mode = "customer", shellOwned = false }: ChatPageProps) {
  // Shell-hosted mounts (persistent /sales/layout) already sit under the root
  // theme boundary and the layout's antd App context; stacking a second
  // provider pair here would duplicate both. Standalone mounts keep their own.
  const canvas = (
    <ChatCanvas routeProjectKey={routeProjectKey} audience={audience} sessionId={sessionId} mode={mode} shellOwned={shellOwned} />
  );
  return shellOwned ? canvas : (
    <ProThemeProvider>
      <AntApp>{canvas}</AntApp>
    </ProThemeProvider>
  );
}

function ChatCanvas({ routeProjectKey, audience = "customer", sessionId, mode = "customer", shellOwned = false }: ChatPageProps) {
  const { message } = AntApp.useApp();
  const router = useRouter();
  const auth = useOptionalAuth();
  const staffRole = auth?.user?.role;
  const isStaff = staffRole === "sales" || staffRole === "admin";
  // Training mode is the internal workspace: one flag owns every branch so no
  // scattered pathname/quota checks can leak a selling surface into it.
  const isTraining = mode === "training";
  // Every project/session URL write is surface-aware: the sales workspace
  // stays on its canonical /sales/chat/project/* route, the customer
  // storefront on /project/* — regardless of who is signed in.
  const chatSurface: ChatSurface = mode === "sales" || audience === "sales" ? "sales" : "customer";
  // Canvas chrome ownership: when the persistent sales layout hosts the canvas
  // (shellOwned) its AppShell owns the header and nav, so the canvas renders
  // none; the standalone training canvas never shows staff navigation either.
  // A bare staff sales mount that is not hosted by the shell keeps its chrome.
  const usesSalesShell = shellOwned || (isStaff && isTraining);
  // Per-project history (requirement): switching projects swaps the entry —
  // never resets. A project with no entry yet is greeted on first selection.
  // Training seeds its stable intro into its own private scope bucket.
  const [messagesByProject, setMessagesByProject] = useState<Record<string, ChatMessage[]>>(
    (): Record<string, ChatMessage[]> => (isTraining ? { [routeProjectKey ?? FALLBACK_ACTIVE_PROJECTS[0].project_key]: [TRAINING_INTRO_MESSAGE] } : {})
  );
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  // Training auth gate (training 401 hardening): latched when a training send
  // resolves a null Firebase bearer — that send never reaches /query. The
  // latch carries the auth uid AT TRIP TIME ("" = tripped while
  // unauthenticated, the auth-init race); the render derives the live gate
  // from it, so a race trip self-heals when a user materializes without any
  // setState-in-effect (react-hooks/set-state-in-effect).
  const [trainingAuthExpiredFor, setTrainingAuthExpiredFor] = useState<
    string | null
  >(null);
  const sessionIdRef = useRef<string>("");
  const hydratedSessionRef = useRef<string | null>(null);
  // Per-session hydration attempt ledger for the bounded deep-link retry
  // (MAX_SESSION_HYDRATION_ATTEMPTS); keyed by session id so a genuinely new
  // deep link always gets its full budget.
  const hydrationAttemptsRef = useRef<{ id: string; attempts: number }>({ id: "", attempts: 0 });
  const tokenBufferRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);
  // Static catalog shows instantly; live SSE places supersede it.
  const [places, setPlaces] = useState<NearbyPlace[]>(STATIC_PLACES);
  // Map mode defaults to the map view on first render (server + client
  // identical) to avoid a hydration mismatch; the URL is applied after mount.
  const [mapMode, setMapMode] = useState<"map" | "list">(initialMapModeFromUrl());
  // Lead CTA (Story 5.7): the SSE routing event carries lead_cta_hint; the
  // chip under the composer appears once a hint arrives and a lead is not
  // already recorded for this browser.
  const [leadCtaHint, setLeadCtaHint] = useState<string | null>(null);
  const [leadFormOpen, setLeadFormOpen] = useState(false);
  // Residual issue (dismissal contract): once the customer dismisses the
  // forced-open LeadForm during an exhaustion episode, no late in-flight error
  // frame may force it open again behind their back. Every dismissal affordance
  // (close button, cancel, ESC, mask) closes unconditionally; the flag clears
  // as soon as the wall lifts (lead bonus or
  // a project scope whose allowance is intact), restoring normal auto-open.
  const leadFormDismissedRef = useRef(false);
  const [leadDone, setLeadDone] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return Boolean(window.localStorage.getItem(LEAD_ID_STORAGE_KEY));
    } catch {
      return false;
    }
  });
  // Secure wave (US-1/US-4): server-authoritative quota snapshots, stored per
  // project so the badge + hard wall read only the current project's entry —
  // exhausting one project never walls another. quotaExhausted is the hard wall
  // (composer disabled + forced LeadForm) raised by a 429 / SSE
  // ANONYMOUS_QUOTA_EXCEEDED and lifted only by a lead submission that actually
  // grants bonus turns.
  // Story 10.3 + URL routing: the active project comes from the
  // /project/[projectKey] segment (routeProjectKey). The "camellia" literal is
  // only a test/embedded fallback — production mounts always pass a routed key,
  // and bare "/" is gated away before ChatPage ever renders. Training uses its
  // private scope key instead: it never names a project to the backend.
  // Declared before the quota states so the exhausted-mark restore can key off
  // it at init.
  const [projectKey, setProjectKey] = useState<string>(
    () => routeProjectKey ?? (isTraining ? FALLBACK_ACTIVE_PROJECTS[0].project_key : "camellia")
  );
  const [quotaByProject, setQuotaByProject] = useState<Record<string, QuotaState | null>>({});
  // R5 (FR-20): the exhausted-quota snapshot from a previous visit is restored
  // during state INITIALIZATION, not in an effect — the wall (+ its LeadForm
  // button) is present on the very first render after a reload, before any
  // server round trip re-asserts, with no cascading setState pass. Restored
  // once per mount against the initially opened project only (initializers
  // never re-run): mid-session project switches keep their own per-project
  // walls; the mark itself must match BOTH the non-empty anon token AND this
  // project, so a missing/mismatching half restores nothing.
  const [quotaExhaustedByProject, setQuotaExhaustedByProject] = useState<Record<string, boolean>>(() => {
    if (isStaff || isTraining || typeof window === "undefined") return {};
    try {
      const storedToken = getAnonToken(window.localStorage);
      return wasQuotaExhaustedMarked(window.localStorage, storedToken, projectKey)
        ? { [projectKey]: true }
        : {};
    } catch {
      // Storage unavailable (private mode): no restored wall this session.
      return {};
    }
  });
  // Mirror of the persisted anon token so LeadForm can bind the lead (and its
  // one-time bonus) to the same identity that chatted. Initialize lazily so
  // storage hydration does not require a cascading effect update.
  const [anonToken, setAnonToken] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      return getAnonToken(window.localStorage);
    } catch {
      return null;
    }
  });
  // Story 10.1-FE: device_id is the anonymous cross-visit identity, minted once
  // and kept in localStorage so a returning caller is recognized by the backend.
  // Held in state (not a ref) because the LeadForm reads it during render.
  const [deviceId] = useState(() => {
    if (typeof window === "undefined") return "";
    try {
      return getDeviceId(window.localStorage);
    } catch {
      return crypto.randomUUID();
    }
  });
  const [sessionIdState, setSessionIdState] = useState(() => {
    if (typeof window === "undefined") return "";
    try {
      // Scope-aware so a training mount reads its OWN session key and never
      // inherits (or clobbers) the customer conversation's id in the same tab.
      return getSessionIdForScope(window.sessionStorage, isTraining ? "training" : "customer");
    } catch {
      return crypto.randomUUID();
    }
  });
  // Story 10.3 + URL routing: the active project comes from the
  // /project/[projectKey] segment — declared above with the quota states it keys.
  const [activeProjects, setActiveProjects] = useState<ActiveProject[]>(FALLBACK_ACTIVE_PROJECTS);
  // Live mirror of the catalogue for callbacks that must read the CURRENT list
  // when they RUN, not the one captured where they were created: fireGreeting
  // resolves after a network round trip and used to filter sibling project
  // names against the stale mount-time list (suggestion leak regression).
  const activeProjectsRef = useRef<ActiveProject[]>(activeProjects);
  useEffect(() => {
    activeProjectsRef.current = activeProjects;
  }, [activeProjects]);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [greetingSuggestions, setGreetingSuggestions] = useState<string[]>([]);
  // The user question awaiting a project choice; re-sent once a project is picked.
  const pendingQueryRef = useRef<string | null>(null);
  // Abort controller of the in-flight answer stream (review M5): a project
  // switch aborts it so the old project's SSE cannot keep consuming the
  // backend/network after the context reset, and its terminal events cannot
  // race the new project's greeting. Null while no query is streaming.
  const streamAbortRef = useRef<AbortController | null>(null);
  const switchEpochRef = useRef(0);
  const [greetingLoading, setGreetingLoading] = useState(false);
  // Bumped after each completed training turn so an open history drawer re-lists
  // the freshly-persisted session without a manual reopen (training gains one
  // durable session per turn; the customer list is already server-scoped).
  const [historyRefreshToken, setHistoryRefreshToken] = useState(0);

  // Single source of truth for the current project: every per-project value
  // (messages, quota, wall) is derived from the records keyed by projectKey —
  // no parallel state that can drift on a switch.
  const messages = messagesByProject[projectKey] ?? [];
  const quota = quotaByProject[projectKey] ?? null;
  const quotaExhausted = quotaExhaustedByProject[projectKey] ?? false;

  const setMapModeRouted = useCallback(
    (mode: "map" | "list") => {
      setMapMode(mode);
      syncModeUrl(mode);
    },
    []
  );

  // When the user presses back/forward, sync local state from the URL so the
  // map mode follows history without remounting the chat.
  useEffect(() => {
    const onPopState = () => {
      setMapMode(initialMapModeFromUrl());
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // Restore the map mode from the URL query string after mount. Reading the
  // URL here (instead of in a useState initializer) keeps the first render
  // server/client identical, so a deep link (?mode=list) hydrates cleanly,
  // then swaps to the list view without losing chat state.
  // Project-agnostic by-id patch: the target message lives in whichever
  // project's entry, so streamed tokens (greetings, answers) are written to the
  // right project even when the user switches mid-stream.
  const patchMessage = useCallback(
    (id: string, patch: Partial<ChatMessage> | ((prev: ChatMessage) => Partial<ChatMessage>)) => {
      setMessagesByProject((prev) => {
        const targetKey = Object.keys(prev).find((key) =>
          prev[key].some((m) => m.id === id)
        );
        if (targetKey === undefined) return prev;
        return {
          ...prev,
          [targetKey]: prev[targetKey].map((m) => {
            if (m.id !== id) return m;
            return typeof patch === "function" ? { ...m, ...patch(m) } : { ...m, ...patch };
          }),
        };
      });
    },
    []
  );

  useEffect(() => {
    try {
      const currentSessionId = getSessionIdForScope(
        window.sessionStorage,
        isTraining ? "training" : "customer"
      );
      sessionIdRef.current = currentSessionId;
      syncSessionUrl(currentSessionId);
    } catch {
      // Storage unavailable (private mode): mint ephemeral ids so the chat
      // still works; persistence is a progressive enhancement here.
      const fallbackSessionId = sessionIdState || crypto.randomUUID();
      sessionIdRef.current = fallbackSessionId;
      syncSessionUrl(fallbackSessionId);
    }
    warmPrefetchCache();
  }, [sessionIdState, isTraining]);

  // Auth uid at render time — feeds both the gate latch below (trip-time uid)
  // and the render-derived gate; a plain const keeps the recovery out of an
  // effect body (react-hooks/set-state-in-effect).
  const authUid = auth?.user?.uid ?? null;
  // Render-derived live gate: a trip recorded before any user existed (the
  // auth-init race, latched as "") self-heals the moment a uid materializes;
  // a trip recorded FOR the signed-in uid (token refresh failure) stays
  // latched while that same uid is present — the manual re-login panel path.
  const trainingAuthExpired =
    isTraining &&
    trainingAuthExpiredFor !== null &&
    (trainingAuthExpiredFor === "" ? authUid === null : trainingAuthExpiredFor === authUid);
  // install the bearer-token provider so every /query carries a fresh Firebase
  // ID token (resolved per send; null while nobody is signed in keeps the
  // customer anon_token flow byte-identical). Cleared on unmount so the
  // module-level seam never leaks across pages.
  useEffect(() => {
    setQueryAuthTokenProvider(firebaseQueryAuthToken);
    return () => setQueryAuthTokenProvider(null);
  }, []);

  // Unmount hygiene (review M5 follow-up): an in-flight answer stream must not
  // keep consuming backend/network after the page unmounts, and a pending
  // token-flush timer must not fire into a dead component. Project switches
  // and new sessions keep their own explicit abort paths above.
  useEffect(() => {
    return () => {
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
      if (flushTimerRef.current !== null) {
        window.clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
    };
  }, []);

  // Dismissal mark lifecycle: a wall that is gone (lead bonus granted, or the
  // user moved to a project whose allowance is intact) restores the default
  // auto-open behavior for any future exhaustion episode.
  useEffect(() => {
    if (!quotaExhausted) leadFormDismissedRef.current = false;
  }, [quotaExhausted]);

  // Builds + streams the first-open greeting scoped to one project. The
  // session latch (ragre.hello_shown) guards the mount path against StrictMode's
  // double-mount in dev; project switches bypass it (force) so the freshly
  // chosen project's intro always renders after the context resets.
  const fireGreeting = useCallback(
    (projectKeyForGreeting: string, opts?: { force?: boolean }) => {
      if (typeof window === "undefined") return;
      const force = opts?.force ?? false;
      if (!force) {
        let alreadyShown = false;
        try {
          alreadyShown = window.sessionStorage.getItem(HELLO_SHOWN_KEY) === "1";
        } catch {
          alreadyShown = false;
        }
        if (alreadyShown) return;
      }
      try {
        window.sessionStorage.setItem(HELLO_SHOWN_KEY, "1");
      } catch {
        // sessionStorage unavailable (private mode): non-fatal.
      }

      const epoch = switchEpochRef.current;
      setGreetingLoading(true);
      void fetchGreeting(projectKeyForGreeting, { deviceId, audience })
        .then((payload) => {
          if (switchEpochRef.current !== epoch) return;
          // The catalogue may have landed while the greeting was in flight:
          // read the live list via ref, never the stale closure, so a
          // sibling project that became known after mount is still filtered.
          const safeSuggestions = sanitizeGreetingSuggestions(
            payload.suggestions,
            activeProjectsRef.current,
            projectKeyForGreeting
          );
          const greeting = typeof payload.greeting === "string" && payload.greeting.trim()
            ? payload.greeting
            : null;
          if (!greeting) {
            setGreetingLoading(false);
            return;
          }
          setGreetingSuggestions(safeSuggestions);
          setMessagesByProject((prev) => ({
            ...prev,
            [projectKeyForGreeting]: [
              {
                id: newId(),
                role: "assistant",
                content: greeting,
                images: payload.images ?? [],
                videos: payload.videos ?? [],
              },
              ...(prev[projectKeyForGreeting] ?? []),
            ],
          }));
          setGreetingLoading(false);
        })
        .catch(() => {
          if (switchEpochRef.current === epoch) setGreetingLoading(false);
        });
    },
    [audience, deviceId]
  );

  // A session deep link owns the initial render: hydrate it before any greeting
  // so a shared transcript never receives an unrelated first-visit message.
  useEffect(() => {
    if (!sessionId || !deviceId) return;
    // FR-22 route authority: on /project/* the ROUTE segment — not mutable
    // local state — is the sole project authority for what this deep link may
    // fetch and where its transcript lands. The request's project_key and the
    // bucket key both bind to it; the backend then rejects a foreign-session
    // lookup (404) so another project's transcript can never render here.
    const scopeKey = routeProjectKey ?? projectKey;
    const tracked = hydrationAttemptsRef.current;
    // The retry ledger stays keyed per SESSION id (R4): moving the same
    // deep link across projects (camellia -> soleil) is a legitimate fresh
    // lookup against a newly-scoped backend query and gets its own budget.
    const attempts = tracked.id === sessionId ? tracked.attempts : 0;
    // hydratedSessionRef is the SUCCESS / permanent-rejection latch ONLY — it is
    // never set at dispatch. A re-run (StrictMode double-mount, or any dep
    // churn) must be free to issue a fresh fetch for the same scope:session,
    // bounded by the per-session attempt ledger below; an in-flight or failed
    // fetch must not leave a latch that strands the transcript unrendered.
    if (
      hydratedSessionRef.current === `${scopeKey}:${sessionId}` ||
      attempts >= MAX_SESSION_HYDRATION_ATTEMPTS
    ) {
      return;
    }
    hydrationAttemptsRef.current = { id: sessionId, attempts: attempts + 1 };
    sessionIdRef.current = sessionId;
    syncSessionUrl(sessionId);
    // R4 race guard: snapshot the switch epoch + project scope AT DISPATCH so a
    // Camellia->Soleil switch while this fetch is in flight cannot land the
    // old session's transcript over the new project's greeting. The in-flight
    // guard below also denies landing when the route has moved on.
    const epoch = switchEpochRef.current;
    let storedAnonToken: string | null = null;
    try {
      storedAnonToken = getAnonToken(window.localStorage);
    } catch {
      storedAnonToken = null;
    }
    let cancelled = false;
    if (isTraining) {
      // Training deep links resolve against the training endpoint (the customer
      // session store never holds them). A stale/unauthorized/cross-project id
      // degrades silently to the stable intro: clear the URL param, keep the
      // intro, and never toast — the training surface has no guest fallback.
      void fetchTrainingSession(sessionId)
        .then((detail) => {
          if (cancelled || switchEpochRef.current !== epoch) return;
          const landedScopeKey = routeProjectKey ?? projectKey;
          if (landedScopeKey !== scopeKey || projectKey !== scopeKey) {
            // Route/state drifted from the dispatched scope while in flight:
            // this run must not land. Do NOT touch the success latch — a
            // concurrent run may already have latched it, and clearing here
            // would strand the transcript. The attempt ledger bounds any retry.
            return;
          }
          const contextProjectKey = detail.context?.project_key ?? null;
          // Adopt the transcript's own project when the route supplies no
          // explicit project context (the /sales/train deep-link case): the
          // fetch is owner-gated by the bearer, so the returned context is
          // trustworthy and must become the active training scope rather than
          // being rejected against the default fallback project.
          const adoptedKey =
            !routeProjectKey && contextProjectKey && contextProjectKey !== scopeKey
              ? contextProjectKey
              : null;
          if (contextProjectKey && contextProjectKey !== scopeKey && !adoptedKey) {
            // An explicit project context (routed project / chosen selection)
            // disagrees with the transcript's owner: not this project's
            // transcript — drop the deep link quietly and latch so the retry
            // ledger stays bounded.
            clearSessionParamFromUrl();
            hydratedSessionRef.current = `${scopeKey}:${sessionId}`;
            return;
          }
          const transcript = detail.messages ?? [];
          const hydrated = transcript.map((item, index): ChatMessage => ({
            id: `${sessionId}-${index}`,
            role: item.role,
            content: item.content,
            ...(item.meta && typeof item.meta === "object" ? item.meta : {}),
          }));
          // Land the adopted project and its transcript in one commit so the
          // project <Select> and the rendered bucket agree on the same scope.
          // Latch against the ADOPTED key so the projectKey-driven re-run of
          // this effect is a no-op instead of a second fetch.
          if (adoptedKey) {
            hydratedSessionRef.current = `${adoptedKey}:${sessionId}`;
            setProjectKey(adoptedKey);
          } else {
            // Successful landing at the dispatched scope: latch it so a
            // dependency-driven re-run is a no-op instead of a second fetch.
            hydratedSessionRef.current = `${scopeKey}:${sessionId}`;
          }
          setMessagesByProject((previous) => {
            if (adoptedKey) {
              // Adopted scope is always non-empty: the transcript, or the
              // stable intro when the session has no turns yet.
              return {
                ...previous,
                [adoptedKey]: transcript.length > 0 ? hydrated : [TRAINING_INTRO_MESSAGE],
              };
            }
            // Empty transcript keeps the seeded intro rather than wiping it.
            if (transcript.length === 0) return previous;
            return { ...previous, [scopeKey]: hydrated };
          });
        })
        .catch((error: unknown) => {
          // 403/404 (encoded in the thrown status) is a permanent rejection:
          // clear the param, keep the intro, latch to stop the retry loop. Any
          // other failure (network/5xx) resets the latch for a bounded retry.
          const status = httpStatusFromError(error);
          if (status === 403 || status === 404) {
            clearSessionParamFromUrl();
            hydratedSessionRef.current = `${scopeKey}:${sessionId}`;
            return;
          }
          // Transient failure (network/5xx): leave the success latch untouched
          // (it is null unless a concurrent run already landed) so a legitimate
          // dependency change may retry, bounded by the attempt ledger.
        });
      return () => { cancelled = true; };
    }
    void fetchChatSessionMessages(deviceId, sessionId, scopeKey, storedAnonToken)
      .then((transcript) => {
        if (cancelled || switchEpochRef.current !== epoch) return;
        const landedScopeKey = routeProjectKey ?? projectKey;
        if (landedScopeKey !== scopeKey || projectKey !== scopeKey) {
          // FR-22: the route/state drifted away from the dispatched project
          // while this fetch was in flight — skip with a structured log
          // instead of rendering cross-project content.
          console.warn(
            JSON.stringify({
              event: "fr22_deep_link_hydration_skipped",
              dispatched_scope: scopeKey,
              landed_scope: landedScopeKey,
              rendered_scope: projectKey,
              session_id: sessionId,
            })
          );
          return;
        }
        // Successful landing at the dispatched scope: latch it so a
        // dependency-driven re-run is a no-op instead of a second fetch.
        hydratedSessionRef.current = `${scopeKey}:${sessionId}`;
        const hydrated = transcript.map((item, index): ChatMessage => ({
          id: `${sessionId}-${index}`,
          role: item.role,
          content: item.content,
          ...(item.meta && typeof item.meta === "object" ? item.meta : {}),
        }));
        setMessagesByProject((previous) => {
          // R4 empty-session guard: deep-link hydration of a session the
          // backend has no messages for must never wipe a bucket already
          // populated by newer content (greeting or user turns). Only real
          // transcript content is authoritative enough to replace it.
          if (transcript.length === 0 && (previous[scopeKey]?.length ?? 0) > 0) return previous;
          return { ...previous, [scopeKey]: hydrated };
        });
      })
      .catch(() => {
        // A stale, unauthorized, or cross-project deep link degrades to a
        // normal guest chat. Leave the success latch untouched (it is null
        // unless a concurrent run already landed) so a legitimate dependency
        // change may retry, while the per-session attempt cap keeps this
        // bounded (never a loop, never a stuck latch).
      });
    return () => { cancelled = true; };
  }, [deviceId, isTraining, projectKey, routeProjectKey, sessionId]);

  // Cold-load greeting is skipped for a deep-linked transcript and remains
  // guarded by session storage for ordinary first visits. Training never
  // greets: its stable intro message is the first paint.
  const initialProjectKeyRef = useRef(projectKey);
  useEffect(() => {
    if (!sessionId && !isTraining) fireGreeting(initialProjectKeyRef.current);
  }, [fireGreeting, sessionId, isTraining]);

  // Resolve the active-project list once for the picker and header. It never
  // auto-opens the picker (requirement): cold load starts on the default
  // project and every switch is user-driven via the picker entry point.
  useEffect(() => {
    let cancelled = false;
    void loadActiveProjects()
      .then((projects) => {
        if (cancelled) return;
        const sorted = sortActiveProjects(projects);
        setActiveProjects(sorted);
        if (isTraining && projectKey === TRAINING_SCOPE_KEY && sorted[0]) {
          setProjectKey(sorted[0].project_key);
        }
      })
      .catch(() => {
        // Every source failed (endpoint + fallback): keep the static catalogue;
        // the picker still works from the fallback list.
      });
    return () => {
      cancelled = true;
    };
  }, [isTraining, projectKey]);

  // Opens the ProjectPicker INSTANTLY (optimistic UI): the modal mounts on the
  // already-resolved list (mount effect result or static fallback, never empty)
  // while a background refresh keeps the rows current. The GET /api/projects
  // round trip takes seconds on a cold backend, so awaiting it here used to
  // delay the popup itself. The 422 body, when supplied, stays authoritative.
  const openProjectPicker = useCallback((errorBodyProjects?: unknown) => {
    setProjectPickerOpen(true);
    if (errorBodyProjects !== undefined) {
      void loadActiveProjects({ projects: errorBodyProjects }).then(setActiveProjects);
      return;
    }
    void loadActiveProjects()
      .then(setActiveProjects)
      .catch(() => undefined);
  }, []);

  // Self-heal write path (spec §5.6): every server-returned anon token
  // (JSON response, SSE ack/done, mint endpoint) replaces the stored one.
  const rememberAnonToken = useCallback((token: unknown): void => {
    try {
      if (persistAnonToken(window.localStorage, token)) {
        setAnonToken(token as string);
        return;
      }
    } catch {
      // Storage unavailable (private mode): fall through to memory-only.
    }
    if (typeof token === "string" && token.length > 0) setAnonToken(token);
  }, []);

  // Returns the durable anon identity for the next /query, lazily minting via
  // GET /api/anon/token on the very first interaction (§5.6). Any failure is
  // non-fatal: /query mints-and-returns a token anyway, so the next turn is
  // already keyed correctly.
  const ensureAnonToken = useCallback(async (): Promise<string | undefined> => {
    let stored: string | null = null;
    try {
      stored = getAnonToken(window.localStorage);
    } catch {
      stored = null;
    }
    if (stored) return stored;
    try {
      const response = await fetch(ANON_TOKEN_ENDPOINT, {
        signal: AbortSignal.timeout(ANON_TOKEN_FETCH_TIMEOUT_MS),
      });
      if (!response.ok) return undefined;
      const body = (await response.json()) as { anon_token?: unknown };
      if (typeof body.anon_token !== "string" || body.anon_token.length === 0) return undefined;
      rememberAnonToken(body.anon_token);
      return body.anon_token;
    } catch {
      return undefined;
    }
  }, [rememberAnonToken]);

  const handleSend = useCallback(
    // `targetProjectKey`: explicit scope for a send issued DURING a project
    // switch (the pending resend in applyProjectSwitch). The closure's
    // `projectKey` still holds the OLD key until React re-renders, so a bare
    // handleSend(pending) sent the query to the old project scope AND appended
    // its message pair to the old bucket — the answer vanished behind the
    // newly shown project.
    // `alreadyRetried`: set by the training 409 self-heal when it re-sends the
    // SAME query once after minting a fresh training session id; a second 409
    // then surfaces the server detail instead of looping.
    (text: string, targetProjectKey?: string, alreadyRetried = false) => {
      const query = text.trim();
      const scopeKey = targetProjectKey ?? projectKey;
      // A walled identity (US-4) cannot send: every attempt would be rejected
      // server-side anyway; the LeadForm is the only way forward.
      if (!query || streaming || quotaExhausted) return;

      // A project change can be followed immediately by a send before React
      // commits the state update. Never let that race send an empty session id.
      if (!sessionIdRef.current) {
        try {
          sessionIdRef.current = getSessionIdForScope(
            window.sessionStorage,
            isTraining ? "training" : "customer"
          );
        } catch {
          sessionIdRef.current = crypto.randomUUID();
        }
        setSessionIdState(sessionIdRef.current);
      }

      // The send cycle is a named local so the training 409 self-heal can
      // re-run it (fresh message pair + stream) WITHOUT re-entering the
      // `streaming` guard above: at heal time the guard's captured `streaming`
      // is still true (the terminal commit has not landed), so a plain
      // handleSend() recursion would be blocked. `retried` carries the
      // already-retried flag through the one allowed re-send.
      function performSend(retried: boolean): void {
      const userMsg: ChatMessage = { id: newId(), role: "user", content: query };
      const assistantId = newId();
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        streaming: true,
      };

      // Keep at most MAX_TURNS rounds (each round = 1 user + 1 assistant turn).
      // Drop empty turns and cap each turn's content at 2000 chars to match the
      // backend HistoryTurn.content limit; long RAG answers would otherwise
      // fail validation (HTTP 422) on every turn after the first.
      // History comes from the SCOPE bucket, not the rendered one: on a
      // switch-triggered resend the rendered bucket still belongs to the old
      // project, whose turns must not leak into the new project's context.
      const history = (messagesByProject[scopeKey] ?? [])
        .filter(
          (m) => (m.role === "user" || m.role === "assistant") && m.content.trim().length > 0,
        )
        .slice(-(MAX_TURNS * 2))
        .map((m) => ({ role: m.role, content: m.content.slice(0, 2000) }));

      setMessagesByProject((prev) => ({
        ...prev,
        [scopeKey]: [...(prev[scopeKey] ?? []), userMsg, assistantMsg],
      }));
      setStreaming(true);
      setInput("");

      // One controller per stream; identity-checked below so terminal events
      // of a superseded (aborted) stream cannot flip global streaming state
      // while a newer greeting/query is running (review M5).
      const streamEpoch = switchEpochRef.current;
      const streamAbort = new AbortController();
      streamAbortRef.current = streamAbort;
      const isLatestStream = () => streamAbortRef.current === streamAbort && switchEpochRef.current === streamEpoch;

      const flushTokens = () => {
        if (tokenBufferRef.current) {
          const chunk = tokenBufferRef.current;
          tokenBufferRef.current = "";
          patchMessage(assistantId, (m) => ({ content: m.content + chunk }));
        }
        flushTimerRef.current = null;
      };
      // The stream call is deferred behind the lazy anon-token mint (§5.6) so
      // the very first query of a browser already carries the signed identity.
      // Training skips the anon mint entirely: identity rides the single-mint
      // Firebase bearer credential (bearerOverride) — see the training gate.
      const runStream = (anonTokenForQuery: string | undefined, bearerOverride?: string) =>
        streamQuery(
        {
          query,
          session_id: sessionIdRef.current,
          history,
            ...(isTraining
            ? { answer_mode: "training" as const, context: { project_key: scopeKey } }
            : {
                device_id: deviceId,
                project_key: scopeKey,
                anon_token: anonTokenForQuery,
              }),
          signal: streamAbort.signal,
        },
        {
          onAck: (ackMeta) => {
            patchMessage(assistantId, { acknowledged: true });
            if (isTraining) return;
            const snapshot = normalizeQuota(ackMeta?.quota);
            if (snapshot) {
              setQuotaByProject((prev) => ({ ...prev, [scopeKey]: snapshot }));
            }
            rememberAnonToken(ackMeta?.anon_token);
          },
          onRouting: (payload) => {
            patchMessage(assistantId, { progressStep: 0 });
            // Map panel is always visible, so panel_hint no longer needs to
            // switch the rail; the hint is informational only.
            // Training deliberately never reads lead_cta_hint: a sales CTA
            // must not surface even if the backend still emits the hint.
            if (!isTraining && payload.lead_cta_hint != null) {
              setLeadCtaHint(payload.lead_cta_hint);
            }
          },
          onSources: (sources) => {
            patchMessage(assistantId, { sources, progressStep: 1 });
          },
          onFacts: (facts) => {
            patchMessage(assistantId, { facts, progressStep: 2 });
          },
          onImages: (images) => {
            patchMessage(assistantId, { images });
          },
          onPlaces: (livePlaces) => {
            // Reset to the static catalog when no live places arrive, so a
            // later non-location query does not show stale landmarks.
            setPlaces(livePlaces.length ? livePlaces : STATIC_PLACES);
          },
          onToken: (token) => {
            tokenBufferRef.current += token;
            patchMessage(assistantId, { progressStep: 3 });
            if (flushTimerRef.current === null) {
              flushTimerRef.current = window.setTimeout(flushTokens, 60);
            }
          },
          onDone: (meta) => {
            if (flushTimerRef.current !== null) {
              window.clearTimeout(flushTimerRef.current);
              flushTokens();
            }
            // §5.3/§8: done.answer is the authoritative sanitized answer —
            // it replaces the streamed body so no partial marker survives.
            const projectRedirect = normalizeProjectRedirect(meta.project_redirect);
            patchMessage(assistantId, {
              streaming: false,
              ...(typeof meta.answer === "string" && meta.answer.length > 0
                ? { content: meta.answer }
                : {}),
              confidence: meta.confidence,
              requires_review: meta.requires_review,
              traceId: meta.trace_id,
              latencyMs: meta.latency_ms,
              // Cross-project guardrail: absent/malformed normalizes to null,
              // which renders nothing instead of a dead card.
              ...(projectRedirect ? { projectRedirect } : {}),
            });
            const snapshot = normalizeQuota(meta.quota);
            if (!isTraining && snapshot) {
              setQuotaByProject((prev) => ({ ...prev, [scopeKey]: snapshot }));
            }
            if (!isTraining) rememberAnonToken(meta.anon_token);
            // The durable lead_cta_hint rides the done frame (the routing
            // frame's copy can still be null when the CTA is only warranted
            // after the final answer). Same training guard as onRouting: a
            // sales CTA must never surface in the training workspace.
            const { lead_cta_hint: doneHint } = meta as DoneMeta & {
              lead_cta_hint?: string | null;
            };
            if (!isTraining && doneHint != null) {
              setLeadCtaHint(doneHint);
            }
            // A completed training turn persisted a new session server-side;
            // nudge an open history drawer to re-list. Gated on the live stream
            // so a superseded stream cannot bump the list.
            if (isTraining && isLatestStream()) {
              setHistoryRefreshToken((token) => token + 1);
            }
            if (isLatestStream()) setStreaming(false);
          },
          onError: (err) => {
            if (flushTimerRef.current !== null) {
              window.clearTimeout(flushTimerRef.current);
              flushTokens();
            }
            // Quota wall (US-4): a 429 JSON body or an SSE error frame with
            // code ANONYMOUS_QUOTA_EXCEEDED ends the free allowance. Surface
            // the server's friendly copy, raise the hard gate and force-open
            // the LeadForm — no retry toast, no input restore. Training is
            // unlimited (sales identities): quota frames are ignored there.
            const envelope: QuotaExceededInfo | null = isTraining
              ? null
              : err instanceof QueryRequestError
                ? parseQuotaErrorEnvelope(err.body)
                : err instanceof QueryStreamError
                  ? parseQuotaErrorEnvelope(err.data)
                  : null;
            if (envelope !== null && envelope.code === QUOTA_EXCEEDED_CODE) {
              patchMessage(assistantId, {
                streaming: false,
                content: envelope.message || QUOTA_WALL_FALLBACK_MESSAGE,
              });
              const exhausted = isQuotaExhausted(envelope.quota);
              if (envelope.quota) {
                setQuotaByProject((prev) => ({ ...prev, [scopeKey]: envelope.quota }));
              }
              setQuotaExhaustedByProject((prev) => ({ ...prev, [scopeKey]: exhausted }));
              // R5 (FR-20 amendment): persist the wall so a reload keeps the
              // LeadForm path visible until the server re-asserts (or a lead
              // bonus clears). The mark REQUIRES a non-empty anon token and a
              // non-empty project: if no durable identity exists yet, obtain a
              // stable one first (mint + persist) — never write under the
              // shared empty sentinel. Failure to mint leaves a memory-only
              // wall, which the next authoritative 429 re-marks anyway.
              if (exhausted && scopeKey) {
                void (async () => {
                  let markToken: string | null = null;
                  try {
                    markToken = (await ensureAnonToken()) ?? null;
                  } catch {
                    markToken = null;
                  }
                  if (!markToken) return;
                  try {
                    persistQuotaExhaustedMark(window.localStorage, markToken, scopeKey);
                  } catch {
                    // Storage unavailable (private mode): memory-only wall.
                  }
                })();
              }
              if (!isStaff && shouldForceLeadForm(envelope.quota) && !leadFormDismissedRef.current) {
                setLeadFormOpen(true);
              }
              if (isLatestStream()) setStreaming(false);
              return;
            }
            if (err instanceof QueryRequestError && err.code === "PROJECT_SCOPE") {
              // More than one active project and none chosen: open the picker
              // instead of a dead-end error toast. The question is kept aside
              // and re-sent with the chosen project key.
              patchMessage(assistantId, {
                streaming: false,
                content: "Vui lòng chọn dự án muốn tìm hiểu để tiếp tục.",
              });
              pendingQueryRef.current = query;
              void openProjectPicker(err.body?.projects);
              // The stream IS terminal here: clear the flag so the later
              // resend in applyProjectSwitch passes handleSend's streaming
              // guard (whose closure predates this state update).
              if (isLatestStream()) setStreaming(false);
              return;
            }
            if (
              isTraining &&
              err instanceof QueryRequestError &&
              err.status === 409 &&
              isTrainingSessionConflict(err) &&
              !retried &&
              isLatestStream()
            ) {
              // Defense in depth for the session-collision 409 (root cause: a
              // training turn reusing a customer session id): drop the
              // just-appended pair, mint a fresh training-scope id, tell the
              // user once, and re-run the send exactly once. performSend(true)
              // bypasses the outer streaming guard, whose captured `streaming`
              // is still true at this terminal point (the setStreaming(false)
              // commit has not landed yet).
              setMessagesByProject((prev) => ({
                ...prev,
                [scopeKey]: (prev[scopeKey] ?? []).filter(
                  (m) => m.id !== assistantId && m.id !== userMsg.id
                ),
              }));
              let healedId: string;
              try {
                healedId = getSessionIdForScope(window.sessionStorage, "training", true);
              } catch {
                healedId = crypto.randomUUID();
              }
              sessionIdRef.current = healedId;
              setSessionIdState(healedId);
              syncSessionUrl(healedId);
              setStreaming(false);
              message.warning("Phiên huấn luyện không còn hợp lệ - bắt đầu phiên mới.");
              performSend(true);
              return;
            }
            patchMessage(assistantId, (m) => {
              // Mid-stream cut (proxy/network) before the `done` frame: keep
              // the partial tokens visible in the bubble and offer a retry
              // instead of replacing the answer with a bare error line. Only
              // when the stream died before producing anything does the error
              // copy become the bubble text (no silent empty state).
              const hasPartial = m.content.trim().length > 0;
              // Error-frame contract (additive): an EXPLICIT retryable=true
              // frame takes the new non-interrupted retry path; legacy frames
              // (no flag) fall back to code-based inference but KEEP the
              // interrupted-stream semantics — the pinned "Kết nối bị gián
              // đoạn" Alert contract stays intact for STREAM_TIMEOUT cuts.
              const explicitRetryable = err instanceof QueryStreamError && err.retryable === true;
              const explicitNotRetryable = err instanceof QueryStreamError && err.retryable === false;
              const inferredRetryable =
                !explicitRetryable &&
                !explicitNotRetryable &&
                err instanceof QueryStreamError &&
                err.retryable === null &&
                isInferRetryableStreamError(err.code);
              return {
                streaming: false,
                error: true,
                interrupted: !(explicitRetryable || explicitNotRetryable),
                retryable: explicitRetryable || inferredRetryable,
                retryQuery: query,
                content: hasPartial ? m.content : err.message,
              };
            });
            if (isLatestStream()) {
              message.error(err.message);
              setStreaming(false);
              // Keep the input text so the user can retry without retyping.
              setInput(query);
            }
          },
        },
        // Single-mint discipline: the gate-validated credential above is
        // pinned to THIS request, so the provider seam is never consulted a
        // second time and a sign-out between gate and fetch cannot emit a
        // bearer-less /query (security-wave QC blocker).
        bearerOverride === undefined ? undefined : { bearerToken: bearerOverride }
      );

      // A superseded stream slot (project switched while minting) must not
      // start consuming (review M5 discipline applied to the mint await).
      // Training 401 hardening: the training backend rejects every bearer-less
      // /query with 401, so a training send whose Firebase bearer cannot
      // resolve must never leave the browser. The bearer is resolved per send
      // instead of trusting the auth context: this mount can render without
      // AuthProvider while the provider seam stays the single source of truth
      // for what /query would actually carry.
      void (async () => {
        if (isTraining) {
          const trainingToken = await firebaseQueryAuthToken().catch(() => null);
          // Stream-identity gate BEFORE any state write: a mint that completes
          // after its stream slot was superseded (project/session switch,
          // newer send, or unmount) belongs to a dead stream and must perform
          // NO state writes at all — no latch set, no clear, no toast/panel
          // side effects — otherwise a stale success would clear a live
          // failure latch and a stale failure would re-latch a live session.
          if (streamAbortRef.current !== streamAbort) return;
          if (typeof trainingToken !== "string" || trainingToken.length === 0) {
            // Latch the uid AT TRIP TIME ("" = tripped while unauthenticated,
            // the auth-init race) so the render-derived gate can distinguish a
            // self-healing race trip from a same-uid genuine expiry.
            setTrainingAuthExpiredFor(authUid ?? "");
            message.error("Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại để tiếp tục đào tạo.");
            // Keep the draft so a re-login does not force retyping.
            setInput(query);
            patchMessage(assistantId, { streaming: false });
            setMessagesByProject((prev) => ({
              ...prev,
              [scopeKey]: (prev[scopeKey] ?? []).filter((m) => m.id !== assistantId && m.id !== userMsg.id),
            }));
            // Terminal blocked send: release the streaming lock too. The
            // self-healing latch re-enables the composer on re-login, and a
            // stale streaming flag would keep its send button dead forever.
            if (isLatestStream()) setStreaming(false);
            return;
          }
          // A successful mint clears any prior latch (e.g. a stale expiry).
          setTrainingAuthExpiredFor(null);
          // Single mint: the validated token rides the request itself; the
          // per-request provider seam is not consulted again, so a sign-out
          // landing between this gate and the fetch cannot strip the bearer.
          void runStream(undefined, trainingToken);
          return;
        }
        const anonTokenForQuery = await ensureAnonToken();
        if (streamAbortRef.current !== streamAbort) return;
        void runStream(anonTokenForQuery);
      })();
      }

      performSend(alreadyRetried);
    },
    [messagesByProject, streaming, message, patchMessage, projectKey, openProjectPicker, deviceId, quotaExhausted, ensureAnonToken, rememberAnonToken, isStaff, isTraining, authUid]
  );

  // Interrupted-stream retry (resilience UX): the bubble's retry button re-sends
  // the SAME question through the normal send path, so history shaping, quota
  // gating and stream lifecycle all stay on the one code path.
  const handleRetryInterrupted = useCallback(
    (msg: ChatMessage) => {
      if (!msg.retryQuery) return;
      handleSend(msg.retryQuery);
    },
    [handleSend]
  );

  const handleNewSession = useCallback(() => {
    // R4: a new session is a context reset just like a project switch — bump
    // the epoch so an in-flight deep-link hydration for the OLD session id
    // cannot resurrect its transcript after this reset.
    switchEpochRef.current += 1;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    // Same fallback pattern as applyProjectSwitch: private-mode storage must
    // not crash the reset — an in-memory session id keeps the chat working.
    // Scope-aware so a training "new session" mints a fresh TRAINING id and
    // never touches the customer conversation's id in the same tab.
    try {
      sessionIdRef.current = getSessionIdForScope(
        window.sessionStorage,
        isTraining ? "training" : "customer",
        true
      );
      setSessionIdState(sessionIdRef.current);
    } catch {
      sessionIdRef.current = crypto.randomUUID();
      setSessionIdState(sessionIdRef.current);
    }
    syncSessionUrl(sessionIdRef.current);
    setMessagesByProject((prev) => ({
      ...prev,
      [projectKey]: isTraining ? [TRAINING_INTRO_MESSAGE] : [],
    }));
    setGreetingSuggestions([]);
    setStreaming(false);
    if (!isTraining) fireGreeting(projectKey, { force: true });
  }, [fireGreeting, isTraining, projectKey]);

  // Lead success payoff (US-1/US-4): lift the wall and credit the bonus
  // optimistically — §5.5 only carries quota_bonus_granted, so cap/remaining
  // are bumped locally until the next server ack/done overwrites them.
  const handleLeadSuccess = useCallback(
    (_leadId: number, quotaBonusGranted: number) => {
      setLeadDone(true);
      if (quotaBonusGranted > 0) {
        setQuotaByProject((prev) => ({
          ...prev,
          [projectKey]: applyLeadBonus(prev[projectKey] ?? null, quotaBonusGranted),
        }));
        setQuotaExhaustedByProject((prev) => ({ ...prev, [projectKey]: false }));
        // R5 (FR-20 amendment): a granted bonus lifts the persisted wall row
        // entirely — there is no shared empty sentinel anymore; the read side
        // discards any mark whose token/project does not match, so a reload
        // after the payoff is not re-walled.
        try {
          clearQuotaExhaustedMark(window.localStorage);
        } catch {
          // Storage unavailable (private mode): memory-only lift.
        }
        message.success(`+${quotaBonusGranted} lượt tư vấn miễn phí`);
      }
    },
    [message, projectKey]
  );

  // Last project key this instance applied (mount included), so the route-sync
  // effect below can tell its own optimistic pushes apart from genuinely new
  // external route changes (back/forward, unknown-key redirect).
  const lastAppliedRouteKeyRef = useRef<string | null>(routeProjectKey ?? null);

  // Applies a project switch: persist it and swap the per-project conversation
  // entry — history is preserved, never reset. A project with no entry yet
  // (first visit this session) greets; returning to a visited project just
  // shows its existing history without re-greeting.
  const applyProjectSwitch = useCallback(
    (key: string) => {
      switchEpochRef.current += 1;
      // Abort the previous project's in-flight answer stream before the swap
      // (review M5): it keeps consuming backend/network after the switch
      // otherwise, and its terminal events must not race the new project's
      // greeting. Aborted streams resolve silently (see streamQuery).
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
      setStreaming(false);
      setGreetingLoading(false);
      try {
        storeProjectKey(window.localStorage, key);
      } catch {
        // Storage unavailable (private mode): the choice still applies for the
        // current visit even though it will not survive a reload.
      }
      const isFreshProject = (messagesByProject[key]?.length ?? 0) === 0;
      setProjectKey(key);
      setProjectPickerOpen(false);
      // A fresh session id detaches the new project's context from the old one
      // on the backend (`device_id:session_id` scope key). Scope-aware so a
      // training switch mints a fresh TRAINING id, never the customer's.
      try {
        sessionIdRef.current = getSessionIdForScope(
          window.sessionStorage,
          isTraining ? "training" : "customer",
          true
        );
        setSessionIdState(sessionIdRef.current);
      } catch {
        sessionIdRef.current = crypto.randomUUID();
        setSessionIdState(sessionIdRef.current);
      }
      syncSessionUrl(sessionIdRef.current);
      setLeadCtaHint(null);
      const pending = pendingQueryRef.current;
      pendingQueryRef.current = null;
      if (pending) {
        // The new key rides along EXPLICITLY: the handleSend closure here was
        // built before setProjectKey above, so a bare resend would target the
        // old project scope and its message bucket.
        handleSend(pending, key);
      } else if (isFreshProject) {
        // First visit to this project this session: greet it. Returning to a
        // project with history swaps back without any re-greeting.
        fireGreeting(key, { force: true });
      }
    },
    [handleSend, fireGreeting, messagesByProject, isTraining]
  );

  // URL ↔ state sync: whenever the routed segment MOVES to a new key (back/
  // forward, programmatic redirects), run the same switch routine so the chat
  // stays scoped to exactly what the address bar says. The trigger is the
  // prop changing between effect runs — NOT disagreement with the optimistic
  // ref — because after a picker-driven push the prop still carries the OLD
  // segment until Next finishes navigating; re-running on mere disagreement
  // used to bounce the user back to the previous project and mint a second
  // orphan session id that diverged from the already-pushed URL.
  const previousRouteKeyRef = useRef<string | null>(routeProjectKey ?? null);
  useEffect(() => {
    const previous = previousRouteKeyRef.current;
    previousRouteKeyRef.current = routeProjectKey ?? null;
    if (!routeProjectKey || routeProjectKey === previous) return;
    // FR-22 stale-effect guard: a prop arriving for a move this component
    // itself already applied (optimistic picker push) — or an out-of-order
    // SUPERSEDED push whose target the user has since moved past — must not
    // re-assert an old project. Re-applying those mints a second orphan
    // session id (diverging from the pushed URL) and is exactly how a stale
    // camellia segment could re-assert itself over newer Soleil intent.
    // Only genuinely external moves (back/forward, unknown-key redirect)
    // switch here, so back/forward stays in sync.
    if (lastAppliedRouteKeyRef.current === routeProjectKey) return;
    lastAppliedRouteKeyRef.current = routeProjectKey;
    applyProjectSwitch(routeProjectKey);
  }, [routeProjectKey, applyProjectSwitch]);

  // Unknown /project/<key> segments must never linger: once the backend
  // catalogue resolves, an unrecognized key redirects to the default (FIRST
  // list entry) with a notice. Catalogue unreachable → skip validation rather
  // than guess; a silent hardcoded fallback is forbidden.
  useEffect(() => {
    if (!routeProjectKey) return;
    let cancelled = false;
    void fetchProjectCatalog()
      .then((catalog) => {
        if (cancelled || catalog.length === 0) return;
        const decision = decideRoutedProject(catalog, routeProjectKey);
        if (decision.kind === "accept") return;
        message.warning(
          `Dự án "${routeProjectKey}" không tồn tại hoặc đã đóng bán. Đã chuyển sang dự án mặc định.`
        );
        lastAppliedRouteKeyRef.current = null; // let the route sync re-apply
        router.replace(projectChatUrl(chatSurface, decision.fallbackPath.replace(/^\/project\//, ""), sessionIdRef.current));
      })
      .catch(() => {
        // Endpoint down after retries: keep chatting on the routed key.
      });
    return () => {
      cancelled = true;
    };
  }, [routeProjectKey, message, router, chatSurface]);

  // Picker/redirect-card selection. In routed mode the address bar changes
  // FIRST (immediate navigation), the switch logic applies optimistically for
  // instant feedback, and the route-sync effect skips the duplicate pass via
  // lastAppliedRouteKeyRef. Without a route context (embedded/test mounts) the
  // switch stays local, preserving the pre-routing behavior.
  const handleSelectProject = useCallback(
    (key: string) => {
      setProjectPickerOpen(false);
      // FR-22 rapid-switch stability: re-selecting the project this instance
      // already applied (double activation, lagging route prop) is a no-op —
      // a repeated switch would mint a second session id and fire a duplicate
      // greeting for a context the user never left.
      if (routeProjectKey !== undefined && lastAppliedRouteKeyRef.current === key) return;
      if (routeProjectKey !== undefined) {
        lastAppliedRouteKeyRef.current = key;
        applyProjectSwitch(key);
        void router.push(projectChatUrl(chatSurface, key, sessionIdRef.current));
        return;
      }
      applyProjectSwitch(key);
    },
    [applyProjectSwitch, routeProjectKey, router, chatSurface]
  );

  // R3 (FR-18): a history selection hydrates the MAIN chat canvas through the
  // same canonical write path as the deep link — bucket replacement, canonical
  // session id (ref + state + storage), URL rewrite with the stable history
  // indicator — and never triggers the greeting (the transcript replaces the
  // empty-state content). The epoch bump keeps any in-flight greeting/stream
  // from the previous context from writing over the restored transcript.
  const applyHistorySelection = useCallback(
    (selectedSessionId: string, transcript: ChatMessage[]) => {
      switchEpochRef.current += 1;
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
      setStreaming(false);
      setGreetingLoading(false);
      setLeadCtaHint(null);
      // Session storage stays the single write authority (R2): record the
      // selected id there before mirroring it into ref/state/URL. Scope-aware so
      // a training selection lands on the training key, never the customer's.
      try {
        window.sessionStorage.setItem(
          sessionKeyForScope(isTraining ? "training" : "customer"),
          selectedSessionId
        );
      } catch {
        // Storage unavailable (private mode): in-memory ids keep working.
      }
      sessionIdRef.current = selectedSessionId;
      setSessionIdState(selectedSessionId);
      syncSessionUrl(selectedSessionId);
      hydratedSessionRef.current = selectedSessionId;
      hydrationAttemptsRef.current = { id: selectedSessionId, attempts: 0 };
      setMessagesByProject((prev) => ({ ...prev, [projectKey]: transcript }));
      if (routeProjectKey !== undefined && typeof window !== "undefined") {
        void router.replace(
          buildProjectHistoryUrl(projectKey, selectedSessionId, window.location.search, chatSurface)
        );
      }
    },
    [projectKey, routeProjectKey, router, chatSurface, isTraining]
  );

  // Suggestion clicks from MessageList arrive via the ASK_EVENT custom event.
  // The handler reads the latest handleSend through a ref so the document
  // listener is registered ONCE per mount instead of being torn down and
  // re-added on every streaming token flush (handleSend changes identity each
  // time messagesByProject moves). Output identical, listener churn gone.
  const handleSendRef = useRef(handleSend);
  useEffect(() => {
    handleSendRef.current = handleSend;
  }, [handleSend]);
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail;
      if (typeof detail === "string") handleSendRef.current(detail);
    };
    document.addEventListener(ASK_EVENT, handler);
    return () => document.removeEventListener(ASK_EVENT, handler);
  }, []);

  // The active project name for the header and lead form. The header title
  // uses the compact short_name ("The Soleil") while fuller contexts (subtitle,
  // lead form, aria labels) keep the long display_name. Training overrides
  // both: its header names the internal workspace, not any project.
  const currentProject =
    activeProjects.find((p) => p.project_key === projectKey) ?? null;
  const headerTitle = isTraining
    ? "Đào tạo nội bộ"
    : currentProject
      ? projectShortName(currentProject)
      : "Tư vấn bất động sản";
  const headerSubtitle = isTraining
    ? "Hỏi-đáp tài liệu đào tạo dành cho chuyên viên bán hàng"
    : currentProject
      ? `Chuyên viên tư vấn dự án ${projectShortName(currentProject)}`
      : "Chuyên viên tư vấn bất động sản";

  // Quota badge copy follows the approved policy: anonymous 3 (+5 once),
  // registered customer 5 (+5 once), sales exactly 10, admin unlimited.
  const quotaBadgeText = quota === null ? "" : quotaBadgeLabel(quota);
  // Staff surfaces never reuse the customer lead-bonus CTA, even before the
  // first quota snapshot arrives. Sales remains finite at ten turns; admin is
  // unlimited and therefore never presents a quota wall.
  const showLeadBonus = !isStaff && !isTraining && shouldShowLeadBonus(quota);
  // R5: staff/training identities never see the customer quota wall, even when
  // a stale snapshot or server frame marks them exhausted. A restored (null
  // quota, flagged) state walls too — the very next authoritative ack/done/429
  // snapshot then confirms or lifts it.
  const showQuotaWall =
    !isStaff && !isTraining && quotaExhausted && (quota === null || isQuotaExhausted(quota));

  // The map camera follows the active project: two active projects sit
  // kilometres apart, so the map must fly to the chosen project's coordinates
  // instead of always rendering the default project.
  const mapProject = useMemo(() => {
    if (
      currentProject &&
      typeof currentProject.lat === "number" &&
      typeof currentProject.lng === "number"
    ) {
      return {
        lat: currentProject.lat,
        lng: currentProject.lng,
        name: projectShortName(currentProject),
        fullName: projectDisplayName(currentProject),
        // Feeds the offline-tile fallback panel on the map.
        address: projectDisplayLocation(currentProject),
      };
    }
    return DEFAULT_PROJECT;
  }, [currentProject]);

  return (
    <div
      className={shellOwned ? "app-shell-canvas" : "app-viewport-height"}
      style={{
        display: "flex",
        flexDirection: "column",
        background: C.bg,
      }}
    >
      <header
        className="app-header"
        style={{
          padding: "12px 24px",
          display: usesSalesShell ? "none" : "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "8px 12px",
          flexShrink: 0,
          boxShadow: SHADOW.pop,
          zIndex: 1,
        }}
      >
        <div
          className="hero-badge"
          style={{
            width: 42,
            height: 42,
            borderRadius: RADIUS.small,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 20,
            flexShrink: 0,
          }}
        >
          <SafetyCertificateOutlined />
        </div>
        <div style={{ minWidth: 0, overflow: "hidden", flex: "1 1 160px" }}>
          <Typography.Title
            level={4}
            style={{
              margin: 0,
              color: HEADER.title,
              fontSize: 17,
              lineHeight: "24px",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {headerTitle}
          </Typography.Title>
          <Typography.Text
            style={{
              fontSize: 12,
              color: HEADER.subtitle,
              display: "flex",
              alignItems: "center",
              gap: 6,
              minWidth: 0,
              overflow: "hidden",
            }}
          >
            <span
              style={{
                minWidth: 0,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {headerSubtitle}
            </span>
            <span
              style={{
                flexShrink: 0,
                background: HEADER.accentFill,
                color: HEADER.badgeText,
                borderRadius: RADIUS.pill,
                padding: "1px 8px",
                fontSize: 11,
                fontWeight: 600,
              }}
            >
              AI hỗ trợ
            </span>
          </Typography.Text>
        </div>
        <div
          style={{
            marginLeft: "auto",
            display: "flex",
            alignItems: "center",
            gap: 10,
            flexWrap: "wrap",
            justifyContent: "flex-end",
            minWidth: 0,
            maxWidth: "100%",
          }}
        >
          {/* Training has no quota surface at all: sales users are unlimited
              there, so no badge and no wall can ever appear. */}
          {!isTraining && quota !== null && (
            <div
              role="status"
              aria-label={`Lượt tư vấn: ${quotaBadgeText}`}
              className="header-chip"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                borderRadius: RADIUS.pill,
                padding: "6px 12px",
                flexShrink: 0,
              }}
            >
              {quota.isAuthenticated ? (
                <CrownOutlined style={{ color: C.gold, fontSize: 14, flexShrink: 0 }} aria-hidden="true" />
              ) : (
                <ThunderboltOutlined style={{ color: C.gold, fontSize: 14, flexShrink: 0 }} aria-hidden="true" />
              )}
              <span
                style={{
                  fontSize: 13,
                  fontWeight: 600,
                  color: HEADER.chipText,
                  whiteSpace: "nowrap",
                }}
              >
                {quotaBadgeText}
              </span>
            </div>
          )}
          {/* Hot-contact CTA (FR-24 / R8): a real, keyboard-focusable action
              for customers — opens LeadForm so a specialist calls back (the
              per-project hotline lives server-side and is not exposed to the
              FE). The placeholder phone number badge is gone. Staff surfaces
              never carry customer lead-capture semantics: no chip at all. */}
          {isStaff || isTraining ? null : (
            <button
              type="button"
              className="btn-terracotta header-hotline-chip"
              onClick={() => setLeadFormOpen(true)}
              aria-haspopup="dialog"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                minHeight: 40,
                borderRadius: RADIUS.pill,
                padding: "6px 14px",
                flexShrink: 0,
              }}
            >
              <PhoneOutlined aria-hidden="true" />
              <span>Gọi tư vấn</span>
            </button>
          )}
          {/* Training hides the project chip entirely: the workspace is not
              scoped to any project and must never imply one. */}
          {!isTraining ? (
            <div
              role="status"
              aria-label={
                currentProject
                  ? `Dự án đang tư vấn: ${projectDisplayName(currentProject)}`
                  : "Chưa chọn dự án"
              }
              className="header-chip header-chip--accent"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                maxWidth: 320,
                minWidth: 0,
                borderRadius: RADIUS.pill,
                padding: "6px 14px",
              }}
            >
              <EnvironmentOutlined style={{ color: C.gold, fontSize: 14, flexShrink: 0 }} />
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 700,
                  color: HEADER.accentText,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {currentProject ? projectShortName(currentProject) : "Chưa chọn dự án"}
              </span>
              {currentProject?.is_hot ? (
                <span
                  className="chip-gold"
                  style={{
                    borderRadius: RADIUS.pill,
                    padding: "1px 8px",
                    fontSize: 11,
                    fontWeight: 700,
                    flexShrink: 0,
                  }}
                >
                  Nổi bật
                </span>
              ) : null}
            </div>
          ) : null}
          {/* Role-aware navigation shared with the CRM AppShell: staff (and
              training) surfaces navigate between CRM / sales chat / admin via
              the SAME routesForRole source, so the chat header is the shell —
              no second AppShell frame gets stacked around the chat. */}
          {isStaff ? (
            <nav
              aria-label="Điều hướng chính"
              style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}
            >
              {routesForRole(staffRole ?? null).map((route) => (
                <Link key={route.href} href={route.href} className="app-shell__nav-link">
                  {route.href === "/sales/leads" ? (
                    <TeamOutlined aria-hidden="true" />
                  ) : route.href === "/sales/chat" ? (
                    <MessageOutlined aria-hidden="true" />
                  ) : route.href === "/sales/train" ? (
                    <ReadOutlined aria-hidden="true" />
                  ) : (
                    <SafetyCertificateOutlined aria-hidden="true" />
                  )}
                  <span>{route.label}</span>
                </Link>
              ))}
            </nav>
          ) : null}
          <AccountControls />
          {!isTraining && !usesSalesShell ? (
            <Button
              type="default"
              onClick={() => void openProjectPicker()}
              icon={<SwapOutlined />}
              style={{
                height: 40,
                fontSize: 15,
                fontWeight: 600,
                borderRadius: RADIUS.btn,
                borderColor: HEADER.accentChipBorder,
                color: C.goldHover,
                background: "transparent",
              }}
            >
              Đổi dự án
            </Button>
          ) : null}
          <AccessibilityControls />
        </div>
      </header>

      {usesSalesShell && !isTraining ? (
        <div style={{ display: "flex", justifyContent: "flex-end", padding: "12px 24px 0", flexShrink: 0 }}>
          <Button
            type="default"
            onClick={() => void openProjectPicker()}
            icon={<SwapOutlined />}
            aria-label="Đổi dự án"
            style={{
              height: 40,
              fontSize: 15,
              fontWeight: 600,
              borderRadius: RADIUS.btn,
              borderColor: HEADER.accentChipBorder,
              color: C.goldHover,
              background: "transparent",
            }}
          >
            Đổi dự án
          </Button>
        </div>
      ) : null}

      {isTraining ? (
        <div style={{ padding: "16px 24px 0", flexShrink: 0 }}>
          <label htmlFor="training-project" style={{ display: "block", marginBottom: 6, fontWeight: 600, color: C.text }}>
            Dự án đào tạo
          </label>
          <Select
            id="training-project"
            aria-label="Dự án đào tạo"
            value={activeProjects.some((project) => project.project_key === projectKey) ? projectKey : undefined}
            placeholder="Chọn dự án"
            options={activeProjects.map((project) => ({ value: project.project_key, label: projectDisplayName(project) }))}
            onChange={(nextProject) => {
              if (nextProject !== projectKey && messages.length > 1 && !window.confirm("Đổi dự án sẽ bắt đầu phiên đào tạo mới. Bạn có muốn tiếp tục không?")) return;
              if (nextProject !== projectKey) {
                setMessagesByProject((previous) => ({ ...previous, [nextProject]: [TRAINING_INTRO_MESSAGE] }));
                try {
                  // Training project change mints a fresh TRAINING-scope id so
                  // the new project's context detaches from the old one.
                  sessionIdRef.current = getSessionIdForScope(window.sessionStorage, "training", true);
                } catch {
                  sessionIdRef.current = crypto.randomUUID();
                }
                setSessionIdState(sessionIdRef.current);
                syncSessionUrl(sessionIdRef.current);
              }
              setProjectKey(nextProject);
            }}
            style={{ maxWidth: 420, width: "100%" }}
          />
        </div>
      ) : null}
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* Column 1: map rail. Always visible and always wide so the canvas
            reads as a balanced peer of the chat column. Training has no map:
            the workspace is document-QA only, so the chat takes the full width. */}
        {!isTraining ? (
          <div className="evidence-rail evidence-rail--wide" style={{ padding: "16px 0 16px 16px" }}>
            <div
              className="rail-min-height"
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 12,
                height: "100%",
              }}
            >
              <div style={{ flex: 1, minHeight: 0 }}>
                <MapPanel
                  places={places}
                  project={mapProject}
                  mode={mapMode}
                  onModeChange={setMapModeRouted}
                />
              </div>
            </div>
          </div>
        ) : null}
        {/* Column 2: chat column */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          <div style={{ display: "flex", justifyContent: "flex-end", padding: "8px 16px 0" }}>
            <ChatHistoryDrawer
              deviceId={deviceId}
              projectKey={projectKey}
              mode={isTraining ? "training" : "customer"}
              anonToken={anonToken}
              onNewSession={handleNewSession}
              refreshToken={historyRefreshToken}
              onSelectSession={applyHistorySelection}
            />
          </div>
          {greetingLoading && messages.length === 0 ? (
            <div
              role="status"
              aria-busy="true"
              aria-label="Đang tải lời chào"
              style={{ padding: "24px 16px", flex: 1 }}
            >
              <div
                aria-hidden="true"
                style={{ maxWidth: 860, margin: "0 auto", color: C.textMuted, fontSize: 22, letterSpacing: 4 }}
              >
                <span className="greeting-dot">.</span><span className="greeting-dot">.</span><span className="greeting-dot">.</span>
              </div>
            </div>
          ) : null}
          <MessageList
            messages={messages}
            suggestions={greetingSuggestions}
            excludedProjectNames={activeProjects
              .filter((project) => project.project_key !== projectKey)
              .flatMap((project) => [projectDisplayName(project), projectShortName(project)])}
            streaming={streaming}
            onRetry={handleRetryInterrupted}
          />
          <div style={{ padding: "12px 16px 10px", flexShrink: 0 }}>
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => handleSend(input)}
              disabled={showQuotaWall || (isTraining && trainingAuthExpired)}
              streaming={streaming}
            />
            {/* Training 401 hardening: the frozen composer pairs with this
                actionable panel (dead session never emits /query — see
                handleSend), so the user gets a re-login path instead of a
                raw error stream. */}
            {isTraining && trainingAuthExpired && (
              <div
                role="alert"
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 10,
                  maxWidth: 860,
                  margin: "10px auto 0",
                  padding: "10px 14px",
                  background: C.goldSoft,
                  border: `1px solid ${C.goldBorder}`,
                  borderRadius: RADIUS.small,
                }}
              >
                <LockFilled
                  style={{ color: C.goldHover, fontSize: 16, marginTop: 3, flexShrink: 0 }}
                  aria-hidden="true"
                />
                <div style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
                  <span style={{ fontSize: 14, lineHeight: "22px", color: C.text }}>
                    Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại để tiếp tục chế độ đào tạo.
                  </span>
                  <Button
                    type="primary"
                    size="small"
                    style={{ alignSelf: "flex-start" }}
                    onClick={() => {
                      // Deep-link back to the exact URL after re-login.
                      const next = `${window.location.pathname}${window.location.search}`;
                      router.push(`/login?next=${encodeURIComponent(next)}`);
                    }}
                  >
                    Đăng nhập lại
                  </Button>
                </div>
              </div>
            )}
            {/* US-4 hard wall: friendly copy under the frozen composer so the
                gate reads as an offer (free turns for a phone number), not a
                dead end. */}
            {showQuotaWall && (
              <div
                role="alert"
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 10,
                  maxWidth: 860,
                  margin: "10px auto 0",
                  padding: "10px 14px",
                  background: C.goldSoft,
                  border: `1px solid ${C.goldBorder}`,
                  borderRadius: RADIUS.small,
                }}
              >
                <LockFilled
                  style={{ color: C.goldHover, fontSize: 16, marginTop: 3, flexShrink: 0 }}
                  aria-hidden="true"
                />
                <div style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
                  <span style={{ fontSize: 14, lineHeight: "22px", color: C.text }}>
                    Anh/chị đã dùng hết lượt tư vấn miễn phí. Để lại số điện thoại trong form dưới đây để nhận
                    thêm lượt miễn phí - chuyên viên sẽ gọi lại trong khoảng 5 phút.
                  </span>
                  {/* R5 (FR-20): the LeadForm entry point is an ALWAYS-PRESENT,
                      keyboard-focusable control with AA contrast, so the wall
                      is reachable even when the forced-open modal was missed
                      (e.g. restored from a reload snapshot). Double activation
                      stays idempotent — setLeadFormOpen(true) twice is one open
                      form; submit throttling remains inside LeadForm. */}
                  <button
                    type="button"
                    onClick={() => setLeadFormOpen(true)}
                    className="btn-terracotta"
                    style={{
                      alignSelf: "flex-start",
                      height: 40,
                      padding: "0 18px",
                      border: "none",
                      borderRadius: RADIUS.pill,
                      fontSize: 15,
                      fontWeight: 700,
                      fontFamily: "inherit",
                      cursor: "pointer",
                      transition: "box-shadow 0.18s ease, transform 0.18s ease",
                    }}
                  >
                    Để lại số điện thoại nhận thêm lượt tư vấn
                  </button>
                </div>
              </div>
            )}
            {/* §5.1 entry point (a). Entry point (b), a CTA inside the
                AffordabilityCard, is out of scope until that card exists. */}
            {leadCtaHint !== null && !leadDone && showLeadBonus && (
              <button
                type="button"
                onClick={() => setLeadFormOpen(true)}
                className="btn-terracotta"
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 8,
                  width: "100%",
                  maxWidth: 860,
                  height: 48,
                  margin: "10px auto 0",
                  padding: "0 16px",
                  border: "none",
                  borderRadius: RADIUS.pill,
                  fontSize: 16,
                  fontWeight: 600,
                  fontFamily: "inherit",
                  cursor: "pointer",
                  transition: "box-shadow 0.18s ease, transform 0.18s ease",
                }}
              >
                <DownloadOutlined aria-hidden="true" />
                Nhận bảng giá + ưu đãi qua điện thoại
              </button>
            )}
            <div style={{ marginTop: 8 }}>
              <Disclaimer />
            </div>
          </div>
        </div>
      </div>
      {/* Training never mounts the customer selling surfaces: no lead form
          (capture or bonus) and no project picker exist in that mode. */}
      {!isTraining ? (
        <LeadForm
          open={leadFormOpen}
          sessionId={sessionIdState}
          deviceId={deviceId}
          projectKey={projectKey}
          projectName={currentProject ? projectDisplayName(currentProject) : undefined}
          notePrefill={buildLeadNote(messages)}
          anonToken={anonToken ?? undefined}
          onClose={() => {
            // Residual issue: dismissal is UNCONDITIONAL — the close button,
            // cancel, ESC and
            // the mask all close the form even while the identity is walled. The
            // wall lives in the disabled composer plus the persistent wall CTA,
            // never in holding the modal open; the dismissed mark stops a late
            // in-flight error frame from auto-reopening against user intent,
            // while every visible CTA can still reopen it voluntarily.
            leadFormDismissedRef.current = true;
            setLeadFormOpen(false);
          }}
          onSuccess={handleLeadSuccess}
        />
      ) : null}
      {!isTraining ? (
        <ProjectPicker
          open={projectPickerOpen}
          projects={activeProjects}
          currentProjectKey={projectKey}
          onSelect={handleSelectProject}
          onClose={() => setProjectPickerOpen(false)}
        />
      ) : null}
    </div>
  );
}
