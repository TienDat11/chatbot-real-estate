"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage } from "./MessageBubble";
import { MessageBubble } from "./MessageBubble";
import { ASK_EVENT } from "@/lib/constants";
import { Empty } from "antd";

interface MessageListProps {
  messages: ChatMessage[];
  streaming: boolean;
}

const SUGGESTIONS = [
  "Thế chấp đất cho ngân hàng cần những giấy tờ gì?",
  "Cầm cố sổ đỏ giấy tay có hợp pháp không?",
  "Chuyển nhượng đất nông nghiệp cần điều kiện gì?",
  "Quy hoạch dự án tại quận 9 hiện trạng thế nào?",
];

/**
 * Scrollable message area. Auto-scrolls to the bottom on new messages or
 * while streaming; shows question suggestions before the first exchange.
 */
export function MessageList({ messages, streaming }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  if (messages.length === 0) {
    return (
      <div
        ref={scrollRef}
        className="chat-scroll"
        style={{ flex: 1, overflowY: "auto", padding: "24px 16px" }}
      >
        <div style={{ maxWidth: 560, margin: "48px auto 0", textAlign: "center" }}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <span style={{ color: "#5B6478" }}>
                Hỏi về văn bản pháp luật, quy hoạch, hồ sơ dự án bất động sản
              </span>
            }
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 16 }}>
            {SUGGESTIONS.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: q }))}
                style={{
                  background: "#FFFFFF",
                  border: "1px solid #E9ECF2",
                  borderRadius: 10,
                  padding: "9px 14px",
                  textAlign: "left",
                  fontSize: 13,
                  color: "#1A2233",
                  cursor: "pointer",
                  boxShadow: "0 1px 2px rgba(26,34,51,0.04)",
                  transition: "border-color .15s, box-shadow .15s",
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.borderColor = "#1F46A8";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.borderColor = "#E9ECF2";
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
