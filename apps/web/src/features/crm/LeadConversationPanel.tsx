"use client";

/**
 * LeadConversationPanel — read-only chat transcript inside the CRM customer
 * drawer (story: sales sees what the customer asked before calling back).
 *
 * Data path: GET /api/crm/leads/{leadId}/conversation with the CRM shell's
 * fresh Firebase bearer token (crmApiClient, NOT lib/api.ts). The backend 404s
 * a lead without a linked chat session — the client resolves that to an empty
 * transcript, which renders as the friendly "chưa chat" state.
 *
 * Rendering follows the answer-style rules: assistant content is markdown-lite
 * (GFM tables + line breaks via MarkdownView), citations collapse to one small
 * "Nguồn: ..." line, and no colored frames/backgrounds wrap the content.
 */
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Spin, Tooltip, Typography } from "antd";
import { CheckOutlined, CopyOutlined, ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { MarkdownView } from "@rag-ragre/ui";
import {
  CrmApiClientError,
  fetchLeadConversation,
  type LeadConversation,
  type LeadConversationMessage,
} from "./crmApiClient";
import { C } from "@/lib/tokens";

export interface LeadConversationPanelProps {
  /** Lead currently open in the drawer; anchors the conversation URL. */
  leadId: string;
  /**
   * Numeric Postgres leads.id when the mirrored lead carries it; the
   * conversation route keys by this int and falls back to the string id.
   */
  numericLeadId?: number;
  /** Fresh Firebase ID token minted by the CRM workspace shell. */
  bearerToken: string | null;
}

/** Titles of the sources cited in an assistant turn ("Nguồn: ..." line). */
function citationTitles(meta: Record<string, unknown> | null): string[] {
  if (meta === null) {
    return [];
  }
  const rawSources = meta.sources;
  if (!Array.isArray(rawSources)) {
    return [];
  }
  return rawSources.flatMap((source) => {
    if (typeof source !== "object" || source === null) {
      return [];
    }
    const title = (source as { title?: unknown }).title;
    return typeof title === "string" ? [title] : [];
  });
}

/** Plain-text copy payload: role prefix keeps speaker attribution in the paste. */
function transcriptCopyText(messages: readonly LeadConversationMessage[]): string {
  return messages
    .map((chatMessage) => {
      const speaker = chatMessage.role === "user" ? "Khách" : "Chatbot";
      return `${speaker}: ${chatMessage.content}`;
    })
    .join("\n\n");
}

export function LeadConversationPanel({
  leadId,
  numericLeadId,
  bearerToken,
}: LeadConversationPanelProps) {
  const [conversation, setConversation] = useState<LeadConversation | null>(
    null
  );
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // Bumping the nonce is the manual refresh path; the effect re-runs on it.
  const [reloadNonce, setReloadNonce] = useState(0);
  // Copy-all feedback; self-resetting so the button returns to its idle icon
  // without any unmount race (timer cleared on the next copy or by the guard).
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle"
  );
  const copyResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // The shell mints the token right after the drawer opens; wait for it
    // instead of firing an unauthenticated request that would 401.
    if (bearerToken === null) {
      return;
    }
    let loadCancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setErrorMessage(null);
    fetchLeadConversation({ leadId, numericLeadId, bearerToken })
      .then((result) => {
        if (!loadCancelled) {
          setConversation(result);
        }
      })
      .catch((error: unknown) => {
        if (!loadCancelled) {
          setErrorMessage(
            error instanceof CrmApiClientError
              ? error.message
              : "Không tải được hội thoại với khách."
          );
        }
      })
      .finally(() => {
        if (!loadCancelled) {
          setLoading(false);
        }
      });
    return () => {
      loadCancelled = true;
    };
  }, [leadId, numericLeadId, bearerToken, reloadNonce]);

  // Newest message sits at the bottom; snap the viewport there after a load.
  useEffect(() => {
    const transcriptElement = transcriptRef.current;
    if (transcriptElement !== null && conversation !== null) {
      transcriptElement.scrollTop = transcriptElement.scrollHeight;
    }
  }, [conversation]);

  const messages = conversation?.messages ?? [];
  // Header meta lets sales confirm at a glance how much was exchanged and how
  // fresh the newest turn is, without scrolling the transcript.
  const newestMessage =
    messages.length > 0 ? messages[messages.length - 1] : null;
  const newestTimestamp =
    newestMessage !== null ? dayjs(newestMessage.created_at) : null;
  const newestTimestampLabel =
    newestTimestamp !== null && newestTimestamp.isValid()
      ? newestTimestamp.format("DD/MM HH:mm")
      : null;

  function scheduleCopyStateReset(): void {
    if (copyResetTimerRef.current !== null) {
      clearTimeout(copyResetTimerRef.current);
    }
    copyResetTimerRef.current = setTimeout(() => setCopyState("idle"), 2000);
  }
  useEffect(() => {
    return () => {
      if (copyResetTimerRef.current !== null) {
        clearTimeout(copyResetTimerRef.current);
      }
    };
  }, []);

  async function handleCopyTranscript(): Promise<void> {
    if (messages.length === 0) {
      return;
    }
    try {
      await navigator.clipboard.writeText(transcriptCopyText(messages));
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    scheduleCopyStateReset();
  }

  return (
    <div
      data-testid="lead-conversation-panel"
      style={{ width: "100%" }}
      aria-label="Hội thoại với khách"
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          marginBottom: 8,
        }}
      >
        <Typography.Text strong>Hội thoại với khách</Typography.Text>
        <span style={{ flexShrink: 0 }}>
          <Tooltip title="Sao chép toàn bộ hội thoại vào clipboard">
            <Button
              size="small"
              icon={copyState === "copied" ? <CheckOutlined /> : <CopyOutlined />}
              loading={false}
              onClick={() => void handleCopyTranscript()}
              aria-label={
                copyState === "copied"
                  ? "Đã sao chép hội thoại"
                  : "Sao chép toàn bộ hội thoại"
              }
              disabled={messages.length === 0}
            >
              {copyState === "copied"
                ? "Đã sao chép"
                : copyState === "failed"
                  ? "Chưa được"
                  : "Sao chép"}
            </Button>
          </Tooltip>{" "}
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => setReloadNonce((previous) => previous + 1)}
            aria-label="Làm mới hội thoại"
          >
            Làm mới
          </Button>
        </span>
      </div>

      {messages.length > 0 ? (
        <Typography.Text
          type="secondary"
          style={{ fontSize: 12, display: "block", marginBottom: 8 }}
          data-testid="conversation-meta"
        >
          {newestTimestampLabel !== null
            ? `${messages.length} tin nhắn · mới nhất ${newestTimestampLabel}`
            : `${messages.length} tin nhắn`}
        </Typography.Text>
      ) : null}

      {errorMessage !== null ? (
        <Alert
          type="error"
          showIcon
          message="Không tải được hội thoại"
          description={errorMessage}
          action={
            <Button
              size="small"
              danger
              onClick={() => setReloadNonce((previous) => previous + 1)}
            >
              Thử lại
            </Button>
          }
        />
      ) : loading && conversation === null ? (
        <div style={{ textAlign: "center", padding: "24px 0" }}>
          <Spin aria-label="Đang tải hội thoại" />
        </div>
      ) : messages.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="Khách chưa chat trước khi để lại số"
        />
      ) : (
        <div
          ref={transcriptRef}
          role="log"
          aria-label="Nội dung hội thoại"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 12,
            maxHeight: 380,
            overflowY: "auto",
            padding: "4px 2px",
          }}
        >
          {messages.map((chatMessage, index) => (
            <TranscriptBubble
              key={`${index}-${chatMessage.role}-${chatMessage.created_at}`}
              role={chatMessage.role}
              content={chatMessage.content}
              citationTitles={citationTitles(chatMessage.meta)}
              createdAt={chatMessage.created_at}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface TranscriptBubbleProps {
  role: "user" | "assistant";
  content: string;
  citationTitles: string[];
  createdAt: string;
}

function TranscriptBubble({
  role,
  content,
  citationTitles: citedTitles,
  createdAt,
}: TranscriptBubbleProps) {
  const isUser = role === "user";
  // Explicit speaker label: alignment alone does not survive narrow drawers
  // or screen readers, so each turn names its speaker ("Khách" / "Chatbot").
  const speakerLabel = isUser ? "Khách" : "Chatbot";
  return (
    <div
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
      }}
    >
      <div style={{ maxWidth: "88%", minWidth: 0 }}>
        <Typography.Text
          type="secondary"
          style={{
            fontSize: 11,
            display: "block",
            marginBottom: 2,
            textAlign: isUser ? "right" : "left",
          }}
        >
          {speakerLabel}
        </Typography.Text>
        <div
          style={{
            background: isUser ? C.primary : C.surface,
            color: isUser ? "#FFFFFF" : undefined,
            border: isUser ? "none" : `1px solid ${C.border}`,
            borderRadius: isUser ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
            padding: "8px 12px",
            fontSize: 13,
            lineHeight: "22px",
            wordBreak: "break-word",
          }}
        >
          {/* Assistant answers keep their markdown-lite shape (GFM tables +
              line breaks); user text stays plain like the customer typed it. */}
          {isUser ? (
            <span style={{ whiteSpace: "pre-wrap" }}>{content}</span>
          ) : (
            <MarkdownView content={content} />
          )}
        </div>
        {!isUser && citedTitles.length > 0 ? (
          <Typography.Text
            type="secondary"
            style={{ fontSize: 11, display: "block", marginTop: 2 }}
          >
            {`Nguồn: ${citedTitles.join(", ")}`}
          </Typography.Text>
        ) : null}
        <Typography.Text
          type="secondary"
          style={{
            fontSize: 10,
            display: "block",
            marginTop: 2,
            textAlign: isUser ? "right" : "left",
          }}
        >
          {dayjs(createdAt).isValid()
            ? dayjs(createdAt).format("DD/MM HH:mm")
            : ""}
        </Typography.Text>
      </div>
    </div>
  );
}
