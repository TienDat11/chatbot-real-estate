"use client";

// antd v5 + React 19 compat patch — must live in the client bundle, before
// any antd component renders.
import "@ant-design/v5-patch-for-react-19";

import { useCallback, useEffect, useRef, useState } from "react";
import { App as AntApp, Typography } from "antd";
import { SafetyCertificateOutlined } from "@ant-design/icons";
import { ThemeProvider, Disclaimer } from "@rag-ragre/ui";
import type { NearbyPlace } from "@rag-ragre/contracts";
import { streamQuery } from "@/lib/api";
import { ASK_EVENT } from "@/lib/constants";
import { GREETING_STATIC_TEXT, GREETING_IMAGES, GREETING_VIDEOS } from "@/lib/greetingContent";
import type { ChatMessage } from "@/components/MessageBubble";
import { AccessibilityControls } from "@/components/AccessibilityControls";
import { warmPrefetchCache } from "@/lib/prefetch";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { MapPanel, DEFAULT_PROJECT } from "./MapPanel";
import { LeadForm, LEAD_ID_STORAGE_KEY } from "./LeadForm";
import { STATIC_PLACES } from "@/lib/places";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

const SESSION_KEY = "ragre.session_id";
const HELLO_SHOWN_KEY = "ragre.hello_shown";
const MAX_TURNS = 4;

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

function getSessionId(): string {
  if (typeof window === "undefined") return "";
  try {
    const existing = window.sessionStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const fresh = crypto.randomUUID();
    window.sessionStorage.setItem(SESSION_KEY, fresh);
    return fresh;
  } catch {
    return crypto.randomUUID();
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
    <ThemeProvider>
      <AntApp>
        <ChatCanvas />
      </AntApp>
    </ThemeProvider>
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
    sessionIdRef.current = getSessionId();
    warmPrefetchCache();
    try {
      if (window.localStorage.getItem(LEAD_ID_STORAGE_KEY)) setLeadDone(true);
    } catch {
      // Storage unavailable (private mode): the chip may reappear; the
      // backend duplicate check still guards the actual submission.
    }
  }, []);

  // First-open greeting: render purely from the static FE greeting config so
  // the intro appears instantly with no network dependency. The copy streams
  // out character-by-character for the typing feel, and the curated images +
  // videos attach to that first message. The sessionStorage flag is claimed
  // synchronously so StrictMode's double-mount in dev never re-runs it.
  useEffect(() => {
    if (typeof window === "undefined") return;
    let alreadyShown = false;
    try {
      alreadyShown = window.sessionStorage.getItem(HELLO_SHOWN_KEY) === "1";
    } catch {
      alreadyShown = false;
    }
    if (alreadyShown) return;
    try {
      window.sessionStorage.setItem(HELLO_SHOWN_KEY, "1");
    } catch {
      // sessionStorage unavailable (private mode): non-fatal.
    }

    const greetingId = newId();
    setMessages((prev) => [
      {
        id: greetingId,
        role: "assistant",
        content: "",
        streaming: true,
        images: GREETING_IMAGES,
        videos: GREETING_VIDEOS,
      },
      ...prev,
    ]);
    setStreaming(true);

    startFakeGreetingStream(
      greetingId,
      GREETING_STATIC_TEXT,
      (patch) => patchMessage(greetingId, patch),
      () => setStreaming(false)
    );
  }, [patchMessage]);

  const handleSend = useCallback(
    (text: string) => {
      const query = text.trim();
      if (!query || streaming) return;

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

      const flushTokens = () => {
        if (tokenBufferRef.current) {
          const chunk = tokenBufferRef.current;
          tokenBufferRef.current = "";
          patchMessage(assistantId, (m) => ({ content: m.content + chunk }));
        }
        flushTimerRef.current = null;
      };
      void streamQuery(
        { query, session_id: sessionIdRef.current, history },
        {
          onAck: () => {
            patchMessage(assistantId, { acknowledged: true });
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
            patchMessage(assistantId, {
              streaming: false,
              confidence: meta.confidence,
              requires_review: meta.requires_review,
              traceId: meta.trace_id,
              latencyMs: meta.latency_ms,
            });
            setStreaming(false);
          },
          onError: (err) => {
            if (flushTimerRef.current !== null) {
              window.clearTimeout(flushTimerRef.current);
              flushTokens();
            }
            patchMessage(assistantId, { streaming: false, error: true, content: err.message });
            message.error(err.message);
            setStreaming(false);
            // Keep the input text so the user can retry without retyping.
            setInput(query);
          },
        }
      );
    },
    [messages, streaming, message, patchMessage, setMapModeRouted]
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
        style={{
          background: C.surface,
          borderBottom: "1px solid " + C.border,
          padding: "10px 24px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexShrink: 0,
          boxShadow: SHADOW.card,
          zIndex: 1,
        }}
      >
        <div
          style={{
            width: 36,
            height: 36,
            borderRadius: RADIUS.small,
            background: C.primary,
            color: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 18,
          }}
        >
          <SafetyCertificateOutlined />
        </div>
        <div style={{ minWidth: 0 }}>
          <Typography.Title level={4} style={{ margin: 0, color: C.text, fontSize: 17, lineHeight: "24px" }}>
            The Camellia
          </Typography.Title>
          <Typography.Text
            style={{
              fontSize: 12,
              color: C.textMuted,
              display: "flex",
              alignItems: "center",
              gap: 6,
              whiteSpace: "nowrap",
            }}
          >
            Chuyên viên tư vấn dự án Sơn Trà, Đà Nẵng
            <span
              style={{
                background: C.successSoft,
                color: C.success,
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
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 13,
              color: C.text,
              background: C.surfaceAlt,
              borderRadius: RADIUS.pill,
              padding: "6px 14px",
            }}
          >
            <span style={{ color: C.primary, fontSize: 14 }}>📞</span>
            <span style={{ fontWeight: 600, letterSpacing: 0.5 }}>09x xxx xxxx</span>
          </div>
          <AccessibilityControls />
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
                project={DEFAULT_PROJECT}
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
              disabled={false}
              streaming={streaming}
            />
            {/* §5.1 entry point (a). Entry point (b), a CTA inside the
                AffordabilityCard, is out of scope until that card exists. */}
            {leadCtaHint !== null && !leadDone && (
              <button
                type="button"
                onClick={() => setLeadFormOpen(true)}
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
                  border: `1px solid ${C.primaryBorder}`,
                  borderRadius: RADIUS.pill,
                  background: C.primarySoft,
                  color: C.primary,
                  fontSize: 16,
                  fontWeight: 600,
                  fontFamily: "inherit",
                  cursor: "pointer",
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
        notePrefill={buildLeadNote(messages)}
        onClose={() => setLeadFormOpen(false)}
        onSuccess={() => setLeadDone(true)}
      />
    </div>
  );
}
