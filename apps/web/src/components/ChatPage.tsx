"use client";

// antd v5 + React 19 compat patch — must live in the client bundle, before
// any antd component renders.
import "@ant-design/v5-patch-for-react-19";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { App as AntApp, Button, Typography } from "antd";
import {
  CrownOutlined,
  EnvironmentOutlined,
  LockFilled,
  SafetyCertificateOutlined,
  SwapOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Disclaimer } from "@rag-ragre/ui";
import type { NearbyPlace } from "@rag-ragre/contracts";
import { ProThemeProvider } from "@/components/ProThemeProvider";
import {
  QueryRequestError,
  QueryStreamError,
  fetchGreetingMedia,
  streamQuery,
} from "@/lib/api";
import { ASK_EVENT } from "@/lib/constants";
import type { ChatMessage } from "@/components/MessageBubble";
import { AccessibilityControls } from "@/components/AccessibilityControls";
import { warmPrefetchCache } from "@/lib/prefetch";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { MapPanel, DEFAULT_PROJECT } from "./MapPanel";
import { LeadForm, LEAD_ID_STORAGE_KEY } from "./LeadForm";
import { STATIC_PLACES } from "@/lib/places";
import { C, RADIUS, SHADOW } from "@/lib/tokens";
import {
  getDeviceId,
  getAnonToken,
  getSessionId,
  getStoredProjectKey,
  persistAnonToken,
  storeProjectKey,
} from "@/features/chat/identity";
import {
  QUOTA_EXCEEDED_CODE,
  applyLeadBonus,
  normalizeQuota,
  parseQuotaErrorEnvelope,
} from "@/features/chat/quota";
import type { QuotaExceededInfo, QuotaState } from "@/features/chat/quota";
import {
  loadActiveProjects,
  FALLBACK_ACTIVE_PROJECTS,
  shouldForceProjectPicker,
  sortActiveProjects,
  projectDisplayName,
} from "@/features/chat/activeProjects";
import type { ActiveProject } from "@/features/chat/activeProjects";
import { ProjectPicker } from "@/features/chat/ProjectPicker";
import { greetingForProject } from "@/features/chat/greeting";

const HELLO_SHOWN_KEY = "ragre.hello_shown";
const MAX_TURNS = 4;

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
  if (window.location.href !== nextUrl) {
    window.history.replaceState(null, "", nextUrl);
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

const GREETING_CHUNK_MS = 40;
const GREETING_CHUNK_SIZE = 3;

/**
 * Streams a greeting string out character-by-character via a timer chain,
 * mimicking a live SSE response so the bot appears to be typing. Cleans up its
 * own interval so no timer leaks after the greeting finishes.
 */
function startFakeGreetingStream(
  messageId: string,
  text: string,
  patch: (p: Partial<ChatMessage>) => void,
  onDone: () => void
): void {
  let cursor = 0;
  const total = text.length;
  const timer = window.setInterval(() => {
    cursor = Math.min(total, cursor + GREETING_CHUNK_SIZE);
    patch({ content: text.slice(0, cursor), streaming: true });
    if (cursor >= total) {
      window.clearInterval(timer);
      patch({ streaming: false });
      onDone();
    }
  }, GREETING_CHUNK_MS);
}

/** Main chat layout: single page with chat and evidence rail side by side. */
export function ChatPage() {
  return (
    <ProThemeProvider>
      <AntApp>
        <ChatCanvas />
      </AntApp>
    </ProThemeProvider>
  );
}

function ChatCanvas() {
  const { message } = AntApp.useApp();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const sessionIdRef = useRef<string>("");
  const tokenBufferRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);
  // Static catalog shows instantly; live SSE places supersede it.
  const [places, setPlaces] = useState<NearbyPlace[]>(STATIC_PLACES);
  // Map mode defaults to the map view on first render (server + client
  // identical) to avoid a hydration mismatch; the URL is applied after mount.
  const [mapMode, setMapMode] = useState<"map" | "list">("map");
  // Lead CTA (Story 5.7): the SSE routing event carries lead_cta_hint; the
  // chip under the composer appears once a hint arrives and a lead is not
  // already recorded for this browser.
  const [leadCtaHint, setLeadCtaHint] = useState<string | null>(null);
  const [leadFormOpen, setLeadFormOpen] = useState(false);
  const [leadDone, setLeadDone] = useState(false);
  // Secure wave (US-1/US-4): server-authoritative quota snapshot for the
  // header badge; quotaExhausted is the hard wall (composer disabled + forced
  // LeadForm) raised by a 429 / SSE ANONYMOUS_QUOTA_EXCEEDED and lifted only
  // by a lead submission that actually grants bonus turns.
  const [quota, setQuota] = useState<QuotaState | null>(null);
  const [quotaExhausted, setQuotaExhausted] = useState(false);
  // Mirror of the persisted anon token so LeadForm can bind the lead (and its
  // one-time bonus) to the same identity that chatted.
  const [anonToken, setAnonToken] = useState<string | null>(null);
  // Story 10.1-FE: device_id is the anonymous cross-visit identity, minted once
  // and kept in localStorage so a returning caller is recognized by the backend.
  // Held in state (not a ref) because the LeadForm reads it during render.
  const [deviceId, setDeviceId] = useState("");
  // Story 10.3: the chosen active project, persisted so the next visit skips
  // the picker; an empty key means the backend will answer 422 PROJECT_SCOPE.
  const [projectKey, setProjectKey] = useState<string>(() => {
    if (typeof window === "undefined") return "";
    try {
      return getStoredProjectKey(window.localStorage) ?? "";
    } catch {
      return "";
    }
  });
  const [activeProjects, setActiveProjects] = useState<ActiveProject[]>(FALLBACK_ACTIVE_PROJECTS);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  // Forced picker state (story 10.1): when several projects are active and the
  // customer has not yet made an explicit choice, the picker opens immediately
  // and no greeting/query is emitted until the choice lands.
  const [projectPickerForced, setProjectPickerForced] = useState(false);
  // True once the active-project list has been resolved (endpoint or fallback);
  // the greeting waits for this so a forced picker can block it entirely.
  const [projectsReady, setProjectsReady] = useState(false);
  // The user question awaiting a project choice; re-sent once a project is picked.
  const pendingQueryRef = useRef<string | null>(null);
  // Abort controller of the in-flight answer stream (review M5): a project
  // switch aborts it so the old project's SSE cannot keep consuming the
  // backend/network after the context reset, and its terminal events cannot
  // race the new project's greeting. Null while no query is streaming.
  const streamAbortRef = useRef<AbortController | null>(null);

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
  useEffect(() => {
    setMapMode(initialMapModeFromUrl());
  }, []);

  const patchMessage = useCallback(
    (id: string, patch: Partial<ChatMessage> | ((prev: ChatMessage) => Partial<ChatMessage>)) => {
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== id) return m;
          return typeof patch === "function" ? { ...m, ...patch(m) } : { ...m, ...patch };
        })
      );
    },
    []
  );

  useEffect(() => {
    try {
      setDeviceId(getDeviceId(window.localStorage));
      sessionIdRef.current = getSessionId(window.sessionStorage);
      setAnonToken(getAnonToken(window.localStorage));
    } catch {
      // Storage unavailable (private mode): mint ephemeral ids so the chat
      // still works; persistence is a progressive enhancement here.
      setDeviceId(crypto.randomUUID());
      sessionIdRef.current = crypto.randomUUID();
    }
    warmPrefetchCache();
    try {
      if (window.localStorage.getItem(LEAD_ID_STORAGE_KEY)) setLeadDone(true);
    } catch {
      // Storage unavailable (private mode): the chip may reappear; the
      // backend duplicate check still guards the actual submission.
    }
  }, []);

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

      const greeting = greetingForProject(projectKeyForGreeting);
      const greetingId = newId();
      setMessages((prev) => [
        {
          id: greetingId,
          role: "assistant",
          content: "",
          streaming: true,
          images: greeting.images,
          videos: greeting.videos,
        },
        ...prev,
      ]);
      setStreaming(true);

      startFakeGreetingStream(
        greetingId,
        greeting.text,
        (patch) => patchMessage(greetingId, patch),
        () => setStreaming(false)
      );

      // Projects without a curated static bundle (Soleil, future registry
      // projects) enrich the greeting with backend media once it arrives —
      // text-first render is never blocked, and any failure is a silent no-op.
      if (greeting.images.length === 0) {
        void fetchGreetingMedia(projectKeyForGreeting)
          .then((media) => {
            if (media.images.length === 0 && media.videos.length === 0) return;
            patchMessage(greetingId, {
              images: media.images,
              videos: media.videos,
            });
          })
          .catch(() => undefined);
      }
    },
    [patchMessage]
  );

  // First-open greeting: rendered purely from the static FE greeting config
  // scoped to the chosen project (a Soleil first-open never greets as Camellia),
  // so the intro is project-consistent with no network dependency. It runs only
  // once the active-project list resolves, and is fully withheld while the
  // picker is forced: a customer who must still choose never sees a greeting
  // that presumes a project.
  useEffect(() => {
    if (!projectsReady) return;
    if (projectPickerForced && !projectKey) return;
    fireGreeting(projectKey);
  }, [projectsReady, projectPickerForced, projectKey, fireGreeting]);

  // Resolve the active-project list once and apply the master-plan picker rule
  // (story 10.1): with more than one active project and no stored explicit
  // choice the picker must open immediately — the FE never silently defaults.
  useEffect(() => {
    let cancelled = false;
    let storedKey: string | null = null;
    try {
      storedKey = getStoredProjectKey(window.localStorage);
    } catch {
      storedKey = null;
    }
    void loadActiveProjects()
      .then((projects) => {
        if (cancelled) return;
        const sorted = sortActiveProjects(projects);
        setActiveProjects(sorted);
        const forced = shouldForceProjectPicker(sorted.length, storedKey);
        setProjectPickerForced(forced);
        if (forced) setProjectPickerOpen(true);
        setProjectsReady(true);
      })
      .catch(() => {
        if (cancelled) return;
        // Every source failed (endpoint + fallback): keep the static catalogue
        // and force nothing; the backend 422 path still raises the picker on
        // demand so the chat never dead-ends silently.
        setProjectsReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
    (text: string) => {
      const query = text.trim();
      // A walled identity (US-4) cannot send: every attempt would be rejected
      // server-side anyway; the LeadForm is the only way forward.
      if (!query || streaming || quotaExhausted) return;

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
      const history = messages
        .filter(
          (m) => (m.role === "user" || m.role === "assistant") && m.content.trim().length > 0,
        )
        .slice(-(MAX_TURNS * 2))
        .map((m) => ({ role: m.role, content: m.content.slice(0, 2000) }));

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setStreaming(true);
      setInput("");

      // One controller per stream; identity-checked below so terminal events
      // of a superseded (aborted) stream cannot flip global streaming state
      // while a newer greeting/query is running (review M5).
      const streamAbort = new AbortController();
      streamAbortRef.current = streamAbort;
      const isLatestStream = () => streamAbortRef.current === streamAbort;

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
      const runStream = (anonTokenForQuery: string | undefined) =>
        streamQuery(
        {
          query,
          session_id: sessionIdRef.current,
          device_id: deviceId,
          project_key: projectKey,
          history,
          anon_token: anonTokenForQuery,
          signal: streamAbort.signal,
        },
        {
          onAck: (ackMeta) => {
            patchMessage(assistantId, { acknowledged: true });
            const snapshot = normalizeQuota(ackMeta?.quota);
            if (snapshot) setQuota(snapshot);
            rememberAnonToken(ackMeta?.anon_token);
          },
          onRouting: (payload) => {
            patchMessage(assistantId, { progressStep: 0 });
            // Map panel is always visible, so panel_hint no longer needs to
            // switch the rail; the hint is informational only.
            if (payload.lead_cta_hint != null) {
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
            patchMessage(assistantId, {
              streaming: false,
              ...(typeof meta.answer === "string" && meta.answer.length > 0
                ? { content: meta.answer }
                : {}),
              confidence: meta.confidence,
              requires_review: meta.requires_review,
              traceId: meta.trace_id,
              latencyMs: meta.latency_ms,
            });
            const snapshot = normalizeQuota(meta.quota);
            if (snapshot) setQuota(snapshot);
            rememberAnonToken(meta.anon_token);
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
            // the LeadForm — no retry toast, no input restore.
            const envelope: QuotaExceededInfo | null =
              err instanceof QueryRequestError
                ? parseQuotaErrorEnvelope(err.body)
                : err instanceof QueryStreamError
                  ? parseQuotaErrorEnvelope(err.data)
                  : null;
            if (envelope !== null && envelope.code === QUOTA_EXCEEDED_CODE) {
              patchMessage(assistantId, {
                streaming: false,
                content: envelope.message || QUOTA_WALL_FALLBACK_MESSAGE,
              });
              if (envelope.quota) setQuota(envelope.quota);
              setQuotaExhausted(true);
              setLeadFormOpen(true);
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
              return;
            }
            patchMessage(assistantId, { streaming: false, error: true, content: err.message });
            if (isLatestStream()) {
              message.error(err.message);
              setStreaming(false);
              // Keep the input text so the user can retry without retyping.
              setInput(query);
            }
          },
        }
      );

      // A superseded stream slot (project switched while minting) must not
      // start consuming (review M5 discipline applied to the mint await).
      void (async () => {
        const anonTokenForQuery = await ensureAnonToken();
        if (streamAbortRef.current !== streamAbort) return;
        void runStream(anonTokenForQuery);
      })();
    },
    [messages, streaming, message, patchMessage, setMapModeRouted, projectKey, openProjectPicker, deviceId, quotaExhausted, ensureAnonToken]
  );

  // Lead success payoff (US-1/US-4): lift the wall and credit the bonus
  // optimistically — §5.5 only carries quota_bonus_granted, so cap/remaining
  // are bumped locally until the next server ack/done overwrites them.
  const handleLeadSuccess = useCallback(
    (_leadId: number, quotaBonusGranted: number) => {
      setLeadDone(true);
      if (quotaBonusGranted > 0) {
        setQuota((prev) => applyLeadBonus(prev, quotaBonusGranted));
        setQuotaExhausted(false);
        message.success(`+${quotaBonusGranted} lượt tư vấn miễn phí`);
      }
    },
    [message]
  );

  // Applies a picked project: persist it, reset the conversation context so the
  // new project answers fresh, then re-send the question that prompted the pick
  // (or greet the freshly chosen project when no question was pending).
  const handleSelectProject = useCallback(
    (key: string) => {
      // Abort the previous project's in-flight answer stream before the
      // context reset (review M5): it keeps consuming backend/network after
      // the switch otherwise, and its terminal events must not race the new
      // project's greeting. Aborted streams resolve silently (see streamQuery).
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
      try {
        storeProjectKey(window.localStorage, key);
      } catch {
        // Storage unavailable (private mode): the choice still applies for the
        // current visit even though it will not survive a reload.
      }
      setProjectKey(key);
      setProjectPickerOpen(false);
      setProjectPickerForced(false);
      // A fresh session id detaches the new project's context from the old one
      // on the backend (`device_id:session_id` scope key).
      try {
        sessionIdRef.current = getSessionId(window.sessionStorage, true);
      } catch {
        sessionIdRef.current = crypto.randomUUID();
      }
      setMessages([]);
      setLeadCtaHint(null);
      const pending = pendingQueryRef.current;
      pendingQueryRef.current = null;
      if (pending) {
        handleSend(pending);
      } else {
        // No question in flight: a forced picker (or a voluntary switch) lands
        // on a fresh conversation, so the intro must follow the chosen project
        // (story 10.1/10.2) — never a stale default project.
        fireGreeting(key, { force: true });
      }
    },
    [handleSend, fireGreeting]
  );

  // Suggestion clicks from MessageList arrive via the ASK_EVENT custom event.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail;
      if (typeof detail === "string") handleSend(detail);
    };
    document.addEventListener(ASK_EVENT, handler);
    return () => document.removeEventListener(ASK_EVENT, handler);
  }, [handleSend]);

  // The active project name for the header and lead form; a project without a
  // stored key (nothing picked yet) reads as a generic welcome line.
  const currentProject =
    activeProjects.find((p) => p.project_key === projectKey) ?? null;
  const headerTitle = currentProject
    ? projectDisplayName(currentProject)
    : "Tư vấn bất động sản";
  const headerSubtitle = currentProject
    ? `Chuyên viên tư vấn dự án ${projectDisplayName(currentProject)}`
    : "Chuyên viên tư vấn bất động sản";

  // Quota badge copy (§5.1 transparency): anonymous visitors see their live
  // remaining allowance; authenticated sales/admin see the unlimited marker.
  const quotaBadgeLabel =
    quota === null
      ? ""
      : quota.isAuthenticated
        ? "Không giới hạn"
        : quota.remainingTurns === null
          ? "Tư vấn miễn phí"
          : quota.remainingTurns > 0
            ? `Còn ${quota.remainingTurns} lượt`
            : "Hết lượt miễn phí";

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
        name: projectDisplayName(currentProject),
      };
    }
    return DEFAULT_PROJECT;
  }, [currentProject]);

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        background: C.bg,
      }}
    >
      <header
        className="app-header"
        style={{
          padding: "12px 24px",
          display: "flex",
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
              color: C.onDark,
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
              color: C.onDarkMuted,
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
                background: "rgba(201, 162, 75, 0.18)",
                color: C.gold,
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
          {quota !== null && (
            <div
              role="status"
              aria-label={`Lượt tư vấn: ${quotaBadgeLabel}`}
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
                  color: C.onDark,
                  whiteSpace: "nowrap",
                }}
              >
                {quotaBadgeLabel}
              </span>
            </div>
          )}
          <div
            className="header-chip"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 13,
              color: C.onDark,
              borderRadius: RADIUS.pill,
              padding: "6px 14px",
              flexShrink: 0,
            }}
          >
            <span style={{ color: C.gold, fontSize: 14 }}>📞</span>
            <span style={{ fontWeight: 600, letterSpacing: 0.5 }}>09x xxx xxxx</span>
          </div>
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
                color: C.onDark,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {currentProject ? projectDisplayName(currentProject) : "Chưa chọn dự án"}
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
          <Button
            type="default"
            onClick={() => void openProjectPicker()}
            icon={<SwapOutlined />}
            style={{
              height: 40,
              fontSize: 15,
              fontWeight: 600,
              borderRadius: RADIUS.btn,
              borderColor: "rgba(201, 162, 75, 0.55)",
              color: C.gold,
              background: "rgba(255,255,255,0.06)",
            }}
          >
            Đổi dự án
          </Button>
          <AccessibilityControls dark />
        </div>
      </header>

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* Column 1: map rail. Always visible and always wide so the canvas
            reads as a balanced peer of the chat column. */}
        <div className="evidence-rail evidence-rail--wide" style={{ padding: "16px 0 16px 16px" }}>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 12,
              height: "100%",
              minHeight: "calc(100vh - 140px)",
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
        {/* Column 2: chat column */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          <MessageList messages={messages} streaming={streaming} />
          <div style={{ padding: "12px 16px 10px", flexShrink: 0 }}>
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => handleSend(input)}
              disabled={quotaExhausted}
              streaming={streaming}
            />
            {/* US-4 hard wall: friendly copy under the frozen composer so the
                gate reads as an offer (free turns for a phone number), not a
                dead end. */}
            {quotaExhausted && (
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
                <span style={{ fontSize: 14, lineHeight: "22px", color: C.text }}>
                  Anh/chị đã dùng hết lượt tư vấn miễn phí. Để lại số điện thoại trong form trên để nhận
                  thêm lượt miễn phí — chuyên viên sẽ gọi lại trong khoảng 5 phút.
                </span>
              </div>
            )}
            {/* §5.1 entry point (a). Entry point (b), a CTA inside the
                AffordabilityCard, is out of scope until that card exists. */}
            {leadCtaHint !== null && !leadDone && (
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
                <span aria-hidden="true">📥</span>
                Nhận bảng giá + ưu đãi qua điện thoại
              </button>
            )}
            <div style={{ marginTop: 8 }}>
              <Disclaimer />
            </div>
          </div>
        </div>
      </div>
      <LeadForm
        open={leadFormOpen}
        sessionId={sessionIdRef.current}
        deviceId={deviceId}
        projectKey={projectKey}
        projectName={currentProject ? projectDisplayName(currentProject) : undefined}
        notePrefill={buildLeadNote(messages)}
        anonToken={anonToken ?? undefined}
        onClose={() => {
          // US-4: the form holds until a lead is actually submitted — ESC,
          // overlay and close button are no-ops while the identity is walled.
          if (!quotaExhausted) setLeadFormOpen(false);
        }}
        onSuccess={handleLeadSuccess}
      />
      <ProjectPicker
        open={projectPickerOpen}
        projects={activeProjects}
        currentProjectKey={projectKey}
        onSelect={handleSelectProject}
        onClose={() => setProjectPickerOpen(false)}
        force={projectPickerForced}
      />
    </div>
  );
}
