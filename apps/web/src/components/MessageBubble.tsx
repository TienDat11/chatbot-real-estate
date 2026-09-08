import type { Confidence, FactEvidence, Image, Source, Video } from "@rag-ragre/contracts";
import { SourcesList, FactsTable, AnswerBlocks } from "@rag-ragre/ui";
import { Alert, App as AntApp, Button, Tooltip, Typography } from "antd";
import { CheckOutlined, CopyOutlined } from "@ant-design/icons";
import { useCallback, useState } from "react";
import { cn, formatLatency } from "@/lib/utils";
import type { ProjectRedirect } from "@/lib/projectRedirect";
import { AckChip } from "./AckChip";
import { ProgressSteps } from "./ProgressSteps";
import { GreetingMedia } from "./GreetingMedia";
import { ProjectRedirectCard } from "./ProjectRedirectCard";
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
  /**
   * Mid-stream failure (proxy/network cut before the `done` frame): the
   * partial answer stays visible and the bubble offers a "Thử lại" retry
   * instead of collapsing into a bare error line.
   */
  interrupted?: boolean;
  /**
   * Error-frame contract (additive): the backend marks timeouts/transient
   * failures retryable. Absent = fall back to `interrupted` semantics.
   */
  retryable?: boolean;
  /** Question to re-send when the user hits the interrupted-stream retry. */
  retryQuery?: string;
  /** Cross-project guardrail: set only when the backend suggests a switch. */
  projectRedirect?: ProjectRedirect;
}

interface MessageBubbleProps {
  message: ChatMessage;
  /** Retry hook for interrupted/retryable streams: re-sends the original question. */
  onRetry?: (message: ChatMessage) => void;
}

/**
 * Staged label shown while the assistant works and no answer text has
 * arrived yet. Rotates with progressStep so the wait reads as a pipeline,
 * not a frozen spinner (blend with AckChip + ProgressSteps, no duplicates).
 */
const WAITING_STAGES = [
  "Đang phân tích câu hỏi",
  "Đang tra cứu tài liệu pháp lý",
  "Đang đối chiếu số liệu dự án",
  "Đang soạn câu trả lời",
];

function WaitingIndicator({ progressStep }: { progressStep?: number }) {
  const stage = WAITING_STAGES[Math.min(progressStep ?? 0, WAITING_STAGES.length - 1)];
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, minHeight: 32 }}>
      <span className="ack-dots" aria-hidden="true" style={{ display: "inline-flex", gap: 3 }}>
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: C.gold }} />
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: C.gold }} />
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: C.gold }} />
      </span>
      <span className="streaming-stage-label">{stage}</span>
    </div>
  );
}

/**
 * Renders a single chat message bubble.
 * - User: right-aligned, navy background, white text.
 * - Assistant: left-aligned, white card with sources, facts, streamed markdown
 *   (typing caret while streaming), then a compact trace footer.
 */
export function MessageBubble({ message, onRetry }: MessageBubbleProps) {
  const isUser = message.role === "user";
  const { message: antdMessage } = AntApp.useApp();
  // The greeting is the only message that carries videos (RAG answers attach
  // images only), so use it to flip the welcome layout: the hero leads with the
  // film + gallery and the static chào text lands last, below the images.
  const isGreeting = !!message.videos?.length;
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      antdMessage.success("Đã sao chép");
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      antdMessage.error("Không thể sao chép nội dung này.");
    }
  }, [antdMessage, message.content]);

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
          message.interrupted || message.retryable ? (
            <>
              <AnswerBlocks
                content={message.content || "Có lỗi xảy ra khi xử lý câu hỏi."}
              />
              <Alert
                type="warning"
                showIcon
                message={message.retryable && !message.interrupted ? "Phản hồi chưa hoàn tất" : "Kết nối bị gián đoạn"}
                style={{ marginTop: 12 }}
                action={
                  <Button
                    size="small"
                    onClick={() => onRetry?.(message)}
                    aria-label="Thử lại câu hỏi"
                  >
                    Thử lại
                  </Button>
                }
              />
            </>
          ) : (
            <Alert
              type="error"
              showIcon
              message="Có lỗi xảy ra"
              description={message.content || "Có lỗi xảy ra khi xử lý câu hỏi."}
              style={{ marginTop: 4 }}
            />
          )
        ) : (
          <>
            {message.sources && message.sources.length > 0 && (
              <SourceSection title="Nguồn tài liệu" sources={message.sources} />
            )}
            {message.facts && message.facts.length > 0 && (
              <FactSection facts={message.facts} />
            )}
            {message.streaming && !message.content ? (
              <div className="streaming-placeholder" aria-live="polite">
                <AckChip visible={!!message.acknowledged} />
                <div style={{ marginTop: 12 }}>
                  <WaitingIndicator progressStep={message.progressStep} />
                </div>
                {message.acknowledged && (
                  <div style={{ marginTop: 12 }}>
                    <ProgressSteps activeStep={message.progressStep ?? 0} />
                  </div>
                )}
              </div>
            ) : (
              <AnswerBlocks
                content={message.content}
                streaming={message.streaming}
                className={cn(message.streaming && "typing-caret")}
              />
            )}
            {(isGreeting || message.videos?.length || message.images?.length) ? (
              <GreetingMedia
                videos={message.videos}
                images={message.images}
                ready={!message.streaming}
              />
            ) : null}
            {/* Cross-project guardrail: prominent switch CTA below the answer
                once the stream is finished; absent field renders nothing. */}
            {message.projectRedirect && !message.streaming && (
              <ProjectRedirectCard redirect={message.projectRedirect} />
            )}
            {!message.streaming && !message.error && message.content.trim().length > 0 && (
              <div
                style={{
                  marginTop: 8,
                  display: "flex",
                  justifyContent: "flex-end",
                }}
                className="bubble-copy-row"
              >
                <Tooltip title={copied ? "Đã sao chép" : "Sao chép câu trả lời"}>
                  <Button
                    type="text"
                    size="small"
                    icon={copied ? <CheckOutlined style={{ color: C.success }} /> : <CopyOutlined />}
                    onClick={handleCopy}
                    aria-label="Sao chép câu trả lời"
                    style={{ color: C.textGhost }}
                  >
                    {copied ? "Đã sao chép" : "Sao chép"}
                  </Button>
                </Tooltip>
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