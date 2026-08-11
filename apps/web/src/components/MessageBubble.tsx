import type { Confidence, FactEvidence, Source } from "@rag-ragre/contracts";
import { ConfidenceBadge, ReviewBanner, SourcesList, FactsTable, MarkdownView } from "@rag-ragre/ui";
import { Typography } from "antd";
import { cn, formatLatency } from "@/lib/utils";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  facts?: FactEvidence[];
  confidence?: Confidence;
  requires_review?: boolean;
  traceId?: string;
  latencyMs?: number;
  streaming?: boolean;
  error?: boolean;
}

interface MessageBubbleProps {
  message: ChatMessage;
}

/**
 * Renders a single chat message bubble.
 * - User: right-aligned, navy background, white text.
 * - Assistant: left-aligned, white card with sources, facts, streamed markdown
 *   (typing caret while streaming), then confidence + review + trace footer.
 */
export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <div
          style={{
            maxWidth: "72%",
            background: "#1F46A8",
            color: "#FFFFFF",
            borderRadius: "14px 14px 4px 14px",
            padding: "10px 14px",
            fontSize: 14,
            lineHeight: "22px",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            boxShadow: "0 1px 3px rgba(31,70,168,0.2)",
          }}
        >
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", justifyContent: "flex-start" }}>
      <div
        style={{
          maxWidth: "86%",
          background: "#FFFFFF",
          border: "1px solid #E9ECF2",
          borderRadius: "14px 14px 14px 4px",
          padding: "14px 16px",
          boxShadow: "0 1px 4px rgba(26,34,51,0.05)",
          width: "100%",
        }}
      >
        {message.error ? (
          <Typography.Text type="danger" style={{ display: "block" }}>
            {message.content || "Có lỗi xảy ra khi xử lý câu hỏi."}
          </Typography.Text>
        ) : (
          <>
            {message.sources && message.sources.length > 0 && (
              <SourceSection title="Nguồn tài liệu" sources={message.sources} />
            )}
            {message.facts && message.facts.length > 0 && (
              <FactSection facts={message.facts} />
            )}
            <MarkdownView
              content={message.content}
              className={cn(message.streaming && "typing-caret")}
            />
            {message.confidence && !message.streaming && (
              <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <ConfidenceBadge confidence={message.confidence} />
                {message.requires_review && <ReviewBanner />}
              </div>
            )}
            {!message.streaming && (message.traceId || message.latencyMs !== undefined) && (
              <div
                style={{
                  marginTop: 10,
                  paddingTop: 8,
                  borderTop: "1px dashed #E9ECF2",
                  color: "#ABB3C3",
                  fontSize: 11,
                  display: "flex",
                  gap: 12,
                  flexWrap: "wrap",
                }}
              >
                {message.traceId && <span>trace_id: {message.traceId}</span>}
                {message.latencyMs !== undefined && (
                  <span>phản hồi trong {formatLatency(message.latencyMs)}</span>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SourceSection({ title, sources }: { title: string; sources: Source[] }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <Typography.Text strong style={{ fontSize: 12, color: "#5B6478", textTransform: "uppercase", letterSpacing: 0.4 }}>
        {title}
      </Typography.Text>
      <div style={{ marginTop: 4 }}>
        <SourcesList sources={sources} max={5} />
      </div>
    </div>
  );
}

function FactSection({ facts }: { facts: FactEvidence[] }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <Typography.Text strong style={{ fontSize: 12, color: "#5B6478", textTransform: "uppercase", letterSpacing: 0.4 }}>
        Sự kiện pháp lý
      </Typography.Text>
      <div style={{ marginTop: 4 }}>
        <FactsTable facts={facts} />
      </div>
    </div>
  );
}
