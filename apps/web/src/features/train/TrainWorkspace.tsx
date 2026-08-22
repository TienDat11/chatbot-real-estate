"use client";

// antd v5 + React 19 compat patch — must live in the client bundle, before
// any antd component renders.
import "@ant-design/v5-patch-for-react-19";

import { useCallback, useEffect, useRef, useState } from "react";
import { App as AntApp, Typography } from "antd";
import { ReadOutlined } from "@ant-design/icons";
import { ThemeProvider } from "@rag-ragre/ui";
import { streamQuery } from "@/lib/api";
import type { ChatMessage } from "@/components/MessageBubble";
import { MessageList } from "@/components/MessageList";
import { Composer } from "@/components/Composer";
import { C, RADIUS, SHADOW } from "@/lib/tokens";
import { getSessionId } from "@/features/chat/identity";

const MAX_TURNS = 4;

const TRAINING_INTRO_MESSAGE: ChatMessage = {
  id: "train-intro",
  role: "assistant",
  content:
    "Chào bạn — đây là chế độ đào tạo nội bộ. Hãy hỏi bất kỳ điều gì về quy trình bán hàng, chính sách dự án hoặc pháp lý bất động sản; câu trả lời luôn kèm trích dẫn tài liệu đào tạo để bạn đối chiếu nguồn.",
};

/**
 * Internal Q&A chat for sales onboarding (story 11.2 / ISSUE-12).
 *
 * Reuses the customer chat frame (MessageList + Composer + streamQuery) but
 * strips every selling surface: no map, no project picker, no lead form and no
 * CTA button. Queries carry answer_mode="training" so the backend (story 11.3)
 * answers from the _training namespace with the coaching prompt; until that
 * lands the field is ignored server-side and errors surface normally.
 */
export function TrainWorkspace() {
  const { message: antdMessage } = AntApp.useApp();
  const [messages, setMessages] = useState<ChatMessage[]>([TRAINING_INTRO_MESSAGE]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const sessionIdRef = useRef<string>("");
  const tokenBufferRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);

  useEffect(() => {
    try {
      sessionIdRef.current = getSessionId(window.sessionStorage);
    } catch {
      // Storage unavailable (private mode): mint an ephemeral id so multi-turn
      // context still works within this visit.
      sessionIdRef.current = crypto.randomUUID();
    }
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

      const assistantId = `train-answer-${Date.now()}`;
      setMessages((prev) => [
        ...prev,
        { id: `train-question-${Date.now()}`, role: "user", content: query },
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);
      setStreaming(true);
      setInput("");

      // Same history cap as the customer chat so long training answers cannot
      // trip the backend HistoryTurn.content limit (HTTP 422) after turn one.
      const history = messages
        .filter((m) => m.role === "user" || m.role === "assistant")
        .filter((m) => !m.error && m.content.trim().length > 0)
        .slice(-(MAX_TURNS * 2))
        .map((m) => ({ role: m.role, content: m.content.slice(0, 2000) }));

      const flushTokens = () => {
        if (tokenBufferRef.current) {
          const chunk = tokenBufferRef.current;
          tokenBufferRef.current = "";
          patchMessage(assistantId, (m) => ({ content: m.content + chunk }));
        }
        flushTimerRef.current = null;
      };

      void streamQuery(
        {
          query,
          session_id: sessionIdRef.current,
          history,
          // Training scope is chosen server-side by answer_mode; project_key
          // stays unset because _training is a reserved key clients may not
          // request directly.
          answer_mode: "training",
        },
        {
          onAck: () => patchMessage(assistantId, { acknowledged: true }),
          onRouting: () => {
            // progressStep only — lead_cta_hint is deliberately unread here:
            // training must never surface a sales CTA even if the backend
            // still emits the hint before story 11.3 turns it off.
            patchMessage(assistantId, { progressStep: 0 });
          },
          onSources: (sources) => {
            patchMessage(assistantId, { sources, progressStep: 1 });
          },
          onFacts: (facts) => {
            patchMessage(assistantId, { facts, progressStep: 2 });
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
            antdMessage.error(err.message);
            setStreaming(false);
            // Keep the input text so the user can retry without retyping.
            setInput(query);
          },
        }
      );
    },
    [messages, streaming, antdMessage, patchMessage]
  );

  return (
    <ThemeProvider>
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
            <ReadOutlined />
          </div>
          <div style={{ minWidth: 0 }}>
            <Typography.Title level={4} style={{ margin: 0, color: C.text, fontSize: 17, lineHeight: "24px" }}>
              Đào tạo nội bộ
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
              Hỏi-đáp tài liệu đào tạo dành cho chuyên viên bán hàng
              <span
                style={{
                  background: C.primarySoft,
                  color: C.primary,
                  border: `1px solid ${C.primaryBorder}`,
                  borderRadius: RADIUS.pill,
                  padding: "1px 8px",
                  fontSize: 11,
                  fontWeight: 600,
                }}
              >
                Chế độ đào tạo
              </span>
            </Typography.Text>
          </div>
        </header>

        <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
          <MessageList messages={messages} streaming={streaming} />
          <div style={{ padding: "12px 16px 10px", flexShrink: 0 }}>
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => handleSend(input)}
              disabled={false}
              streaming={streaming}
            />
          </div>
        </div>
      </div>
    </ThemeProvider>
  );
}
