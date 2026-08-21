import type { Confidence, FactEvidence, Image, Source, Video } from "@rag-ragre/contracts";
import { ConfidenceBadge, ReviewBanner, SourcesList, FactsTable, AnswerBlocks } from "@rag-ragre/ui";
import { Typography } from "antd";
import { cn, formatLatency } from "@/lib/utils";
import { AckChip } from "./AckChip";
import { ProgressSteps } from "./ProgressSteps";
import { ImageGallery } from "./ImageGallery";
import { GreetingMedia } from "./GreetingMedia";
import { C, SHADOW, FS } from "@/lib/tokens";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  facts?: FactEvidence[];
  images?: Image[];
  videos?: Video[];
  confidence?: Confidence;
  requires_review?: boolean;
  traceId?: string;
  latencyMs?: number;
  streaming?: boolean;
  acknowledged?: boolean;
  progressStep?: number;
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
  // The greeting is the only message that carries videos (RAG answers attach
  // images only), so use it to flip the welcome layout: the hero leads with the
  // film + gallery and the static chào text lands last, below the images.
  const isGreeting = !!message.videos?.length;

  if (isUser) {
    return (
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <div
          style={{
            maxWidth: "72%",
            background: C.primary,
            color: "#FFFFFF",
            borderRadius: "16px 16px 4px 16px",
            padding: "10px 16px",
            fontSize: FS.body,
            lineHeight: FS.bodyLine,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            boxShadow: SHADOW.primary,
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
          background: C.surface,
          border: "1px solid " + C.border,
          borderRadius: "16px 16px 16px 4px",
          padding: "16px 18px",
          boxShadow: SHADOW.card,
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
            {/* On the welcome message the media block opens the bubble (hero
                film + gallery first), so the chào copy sits last under the
                images instead of interrupting the cinematic opener. */}
            {isGreeting && (
              <GreetingMedia
                videos={message.videos}
                images={message.images}
                ready={!message.streaming}
              />
            )}
            {message.streaming && !message.content ? (
              <div className="streaming-placeholder">
                <AckChip visible={!!message.acknowledged} />
                {message.acknowledged && (
                  <div style={{ marginTop: 12 }}>
                    <ProgressSteps activeStep={message.progressStep ?? 0} />
                  </div>
                )}
              </div>
            ) : (
              <AnswerBlocks
                content={message.content}
                className={cn(message.streaming && "typing-caret")}
              />
            )}
            {!isGreeting && (message.videos?.length || message.images?.length) ? (
              <GreetingMedia
                videos={message.videos}
                images={message.images}
                ready={!message.streaming}
              />
            ) : null}
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
                  borderTop: "1px dashed " + C.border,
                  color: C.textGhost,
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
      <Typography.Text strong style={{ fontSize: 12, color: C.textMuted, textTransform: "uppercase", letterSpacing: 0.4 }}>
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
      <Typography.Text strong style={{ fontSize: 12, color: C.textMuted, textTransform: "uppercase", letterSpacing: 0.4 }}>
        Sự kiện pháp lý
      </Typography.Text>
      <div style={{ marginTop: 4 }}>
        <FactsTable facts={facts} />
      </div>
    </div>
  );
}