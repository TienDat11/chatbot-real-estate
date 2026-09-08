"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage } from "./MessageBubble";
import { MessageBubble } from "./MessageBubble";
import { ASK_EVENT } from "@/lib/constants";
import { C, RADIUS, SHADOW } from "@/lib/tokens";

interface MessageListProps {
  messages: ChatMessage[];
  streaming: boolean;
  suggestions?: string[];
  excludedProjectNames?: string[];
  /** Retry hook for interrupted/retryable streams: re-sends the original question. */
  onRetry?: (message: ChatMessage) => void;
}

const GENERIC_SUGGESTIONS = [
  "Dự án có những tiện ích gì nổi bật?",
  "Giá và chính sách hiện tại như thế nào?",
  "Vị trí và pháp lý dự án ra sao?",
  "Dự án phù hợp để ở hay đầu tư cho thuê?",
];

/**
 * Scrollable message area. Auto-scrolls to the bottom on new messages or
 * while streaming; shows question suggestions before the first exchange.
 */
export function MessageList({ messages, streaming, suggestions, excludedProjectNames = [], onRetry }: MessageListProps) {
  const sourceSuggestions = suggestions?.length ? suggestions : GENERIC_SUGGESTIONS;
  const visibleSuggestions = sourceSuggestions.filter((suggestion) =>
    !excludedProjectNames.some((name) => suggestion.toLocaleLowerCase().includes(name.toLocaleLowerCase())),
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  // Whether the reader is parked at the bottom. While true we keep following
  // the streaming tail; once the user scrolls up to re-read we stop forcing the
  // caret down so autoscroll never fights their hand.
  const stickToBottomRef = useRef(true);
  // rAF coalescing for streaming token bursts: each token patch re-fires the
  // effect, but only one scroll per animation frame is scheduled, so the
  // scroller never queues dozens of competing tween targets.
  const scrollRafRef = useRef<number | null>(null);
  const prevCountRef = useRef(messages.length);

  useEffect(() => {
    stickToBottomRef.current = true;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    prevCountRef.current = messages.length;
  }, [messages.length]);

  // While streaming with the reader parked at the bottom, follow the tail
  // INSTANTLY: per-token smooth scrolls each start their own tween, and the
  // tween of a burst re-anchors mid-flight, so the pane visibly lurches and
  // slams instead of tracking. Instant assignment (inside the rAF) keeps the
  // pane glued to the growing tail with zero tween state. Smooth gliding is
  // reserved for non-streaming content mutation, where there is no per-token
  // re-anchor problem. Reduced-motion users keep the deterministic instant
  // path; user scroll-up still cancels following via handleScroll.
  // The rAF is cancelled ONLY on unmount: a per-dependency cleanup would run
  // after every streamed token (messages gets a new array identity each token)
  // and cancel the just-scheduled frame before it ever fires, so the
  // guard below would never trip and coalescing would be dead — the pane
  // starves during token bursts and then jumps.
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    if (messages.length !== prevCountRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const instant = streaming || reduceMotion;
    if (scrollRafRef.current !== null) return;
    scrollRafRef.current = window.requestAnimationFrame(() => {
      scrollRafRef.current = null;
      const node = scrollRef.current;
      if (!node || !stickToBottomRef.current) return;
      // Instant path (streaming / reduced motion): direct scrollTop assignment.
      // jsdom (and some old webviews) lack Element.scrollTo — the same
      // assignment degrades gracefully where scrollTo is missing.
      if (instant) {
        node.scrollTop = node.scrollHeight;
        return;
      }
      node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
    });
  }, [messages, streaming]);

  // Unmount-only rAF teardown; see the coalescing note above for why this
  // must not live as the streaming effect's dependency cleanup.
  useEffect(
    () => () => {
      if (scrollRafRef.current !== null) {
        window.cancelAnimationFrame(scrollRafRef.current);
        scrollRafRef.current = null;
      }
    },
    [],
  );

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distanceFromBottom < 80;
  };

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="chat-scroll"
      style={{ flex: 1, overflowY: "auto", padding: "24px 16px" }}
    >
      <div style={{ maxWidth: 860, margin: "0 auto", display: "flex", flexDirection: "column", gap: 16 }}>
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} onRetry={onRetry} />
        ))}
        {messages.length > 0 && !streaming && (
          <div className="suggestion-grid" style={{ display: "grid", gridTemplateColumns: "1fr", gap: 10, textAlign: "left" }}>
            {visibleSuggestions.map((q) => (
              <button key={q} type="button" onClick={() => document.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: q }))} style={{ background: C.surface, border: "1px solid " + C.border, borderRadius: RADIUS.input, padding: "12px 16px", textAlign: "left", fontSize: 14, lineHeight: "22px", color: C.text, cursor: "pointer", boxShadow: SHADOW.card }}>
                {q}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );

}
