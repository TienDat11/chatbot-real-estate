"use client";

// antd v5 + React 19 compat patch — must live in the client bundle, before
// any antd component renders.
import "@ant-design/v5-patch-for-react-19";

import { useCallback, useEffect, useRef, useState } from "react";
import { App as AntApp, Segmented, Typography } from "antd";
import { SafetyCertificateOutlined } from "@ant-design/icons";
import { ThemeProvider, Disclaimer } from "@rag-ragre/ui";
import type { FactEvidence, NearbyPlace, Source } from "@rag-ragre/contracts";
import { streamQuery } from "@/lib/api";
import { ASK_EVENT } from "@/lib/constants";
import type { ChatMessage } from "@/components/MessageBubble";
import { AccessibilityControls } from "@/components/AccessibilityControls";
import { warmPrefetchCache } from "@/lib/prefetch";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { EvidencePanel } from "@/components/EvidencePanel";
import { MapPanel } from "./MapPanel";
import { STATIC_PLACES } from "@/lib/places";

const SESSION_KEY = "ragre.session_id";
const MAX_TURNS = 4;

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
  const [evidence, setEvidence] = useState<{ sources: Source[]; facts: FactEvidence[]; messageId?: string }>({
    sources: [],
    facts: [],
  });
  // Static catalog shows instantly; live SSE places supersede it.
  const [places, setPlaces] = useState<NearbyPlace[]>(STATIC_PLACES);
  const [railTab, setRailTab] = useState<'answer' | 'map'>('answer');

  useEffect(() => {
    sessionIdRef.current = getSessionId();
    warmPrefetchCache();
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
      const history = messages
        .filter((m) => m.role === "user" || m.role === "assistant")
        .slice(-(MAX_TURNS * 2))
        .map((m) => ({ role: m.role, content: m.content }));

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
          onRouting: (r) => {
            patchMessage(assistantId, { progressStep: 0 });
            // Auto-open the map rail when the backend classifies the query as location intent.
            if (r.panel_hint === "map") setRailTab("map");
          },
          onSources: (sources) => {
            patchMessage(assistantId, { sources, progressStep: 1 });
            setEvidence((e) => ({ sources, facts: e.facts, messageId: assistantId }));
          },
          onFacts: (facts) => {
            patchMessage(assistantId, { facts, progressStep: 2 });
            setEvidence((e) => ({ sources: e.sources, facts, messageId: assistantId }));
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
    [messages, streaming, message, patchMessage]
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
        background: "#F7F8FA",
      }}
    >
      <header
        style={{
          background: "#FFFFFF",
          borderBottom: "1px solid #E9ECF2",
          padding: "10px 24px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            width: 34,
            height: 34,
            borderRadius: 10,
            background: "#1F46A8",
            color: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 17,
          }}
        >
          <SafetyCertificateOutlined />
        </div>
        <div>
          <Typography.Title level={4} style={{ margin: 0, color: "#1A2233", fontSize: 16 }}>
            RAG Real Estate
          </Typography.Title>
          <Typography.Text
            style={{
              fontSize: 12,
              color: "#5B6478",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            Tra cứu pháp lý bất động sản
            <span
              style={{
                background: "#EAF7EF",
                color: "#16A34A",
                borderRadius: 999,
                padding: "1px 8px",
                fontSize: 11,
                fontWeight: 600,
              }}
            >
              AI hỗ trợ
            </span>
          </Typography.Text>
        </div>
        <div style={{ marginLeft: "auto" }}>
          <AccessibilityControls />
        </div>
      </header>

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <div className="evidence-rail" style={{ padding: "16px 0 16px 16px" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, height: "100%", minHeight: "calc(100vh - 140px)" }}>
            <Segmented
              block
              value={railTab}
              onChange={(v) => setRailTab(v as "answer" | "map")}
              options={[
                { label: "Trả lời", value: "answer" },
                { label: "Bản đồ", value: "map" },
              ]}
              style={{ height: 48, fontSize: 16, fontWeight: 600 }}
            />
            {railTab === "map" ? (
              <div style={{ flex: 1, minHeight: 0 }}>
                <MapPanel places={places} />
              </div>
            ) : (
              <EvidencePanel
                sources={evidence.sources}
                facts={evidence.facts}
                activeMessageId={evidence.messageId}
              />
            )}
          </div>
        </div>
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          <MessageList messages={messages} streaming={streaming} />
          <div style={{ padding: "12px 16px 8px", flexShrink: 0 }}>
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => handleSend(input)}
              disabled={false}
              streaming={streaming}
            />
            <div style={{ marginTop: 8 }}>
              <Disclaimer />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
