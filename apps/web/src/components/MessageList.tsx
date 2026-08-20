"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage } from "./MessageBubble";
import { MessageBubble } from "./MessageBubble";
import { ASK_EVENT } from "@/lib/constants";
import { Typography } from "antd";
import { SafetyCertificateOutlined } from "@ant-design/icons";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

interface MessageListProps {
  messages: ChatMessage[];
  streaming: boolean;
}

const SUGGESTIONS = [
  "Camellia có những tiện ích gì nổi bật?",
  "Giá căn 2PN view biển hiện tại là bao nhiêu?",
  "Vị trí và pháp lý dự án The Camellia thế nào?",
  "Dự án phù hợp để ở hay đầu tư cho thuê?",
];

/**
 * Scrollable message area. Auto-scrolls to the bottom on new messages or
 * while streaming; shows question suggestions before the first exchange.
 */
export function MessageList({ messages, streaming }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // Whether the reader is parked at the bottom. While true we keep following
  // the streaming tail; once the user scrolls up to re-read we stop forcing the
  // caret down so autoscroll never fights their hand.
  const stickToBottomRef = useRef(true);

  // A brand-new message (greeting, or the user just asked something) always
  // jumps to the latest turn, regardless of where the reader scrolled.
  useEffect(() => {
    stickToBottomRef.current = true;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  // While streaming, keep the caret at the bottom only if the reader has not
  // scrolled up away from it.
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distanceFromBottom < 80;
  };

  if (messages.length === 0) {
    return (
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="chat-scroll"
        style={{ flex: 1, overflowY: "auto", padding: "24px 16px" }}
      >
        <div style={{ maxWidth: 600, margin: "56px auto 0", textAlign: "center" }}>
          <div
            aria-hidden="true"
            style={{
              width: 64,
              height: 64,
              margin: "0 auto 16px",
              borderRadius: RADIUS.card,
              background: C.primarySoft,
              color: C.primary,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 30,
            }}
          >
            <SafetyCertificateOutlined />
          </div>
          <Typography.Title level={3} style={{ margin: 0, color: C.text, fontSize: 22, lineHeight: "30px" }}>
            Tư vấn dự án The Camellia
          </Typography.Title>
          <Typography.Paragraph style={{ color: C.textMuted, fontSize: 15, margin: "8px auto 24px", maxWidth: 420, lineHeight: "24px" }}>
            Hỗ trợ tư vấn căn hộ view biển, view núi Sơn Trà, tiện ích nội khu và
            tra cứu pháp lý, quy hoạch dự án kèm độ tin cậy.
          </Typography.Paragraph>
          <div className="suggestion-grid" style={{ display: "grid", gridTemplateColumns: "1fr", gap: 10, textAlign: "left" }}>
            {SUGGESTIONS.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: q }))}
                style={{
                  background: C.surface,
                  border: "1px solid " + C.border,
                  borderRadius: RADIUS.input,
                  padding: "11px 16px",
                  textAlign: "left",
                  fontSize: 14,
                  lineHeight: "22px",
                  color: C.text,
                  cursor: "pointer",
                  boxShadow: SHADOW.card,
                  transition: "border-color .15s, box-shadow .15s, transform .15s",
                }}
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="chat-scroll"
      style={{ flex: 1, overflowY: "auto", padding: "24px 16px" }}
    >
      <div style={{ maxWidth: 860, margin: "0 auto", display: "flex", flexDirection: "column", gap: 16 }}>
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
      </div>
    </div>
  );
}
