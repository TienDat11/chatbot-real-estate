"use client";

import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Empty, List, Spin, Tag } from "antd";
import { HistoryOutlined } from "@ant-design/icons";
import type { ChatMessage } from "@/components/MessageBubble";
import {
  fetchChatSessionMessages,
  fetchChatSessions,
  fetchTrainingSession,
  fetchTrainingSessions,
  type ChatSessionSummary,
  type TrainingSessionSummary,
} from "@/lib/api";

interface ChatHistoryDrawerProps {
  deviceId: string;
  projectKey: string;
  mode?: "customer" | "training";
  /**
   * Signed anon identity (secure wave §6): every history/session request must
   * carry X-Anon-Token or the backend intentionally rejects ownership with a
   * 401, which this drawer already surfaces as its inline retry alert.
   */
  anonToken: string | null;
  onNewSession: () => void;
  /**
   * Bumped by ChatPage after each completed turn so an OPEN drawer re-lists
   * without a manual reopen (the training workspace gains a session per turn).
   * Ignored while closed; reopening always re-fetches regardless.
   */
  refreshToken?: number;
  /**
   * R3 (FR-18): selecting a session hands the transcript to ChatPage so it
   * hydrates the MAIN chat canvas (bucket write + canonical session + URL
   * update) through the single canonical path; the drawer itself never renders
   * a local transcript copy anymore and simply closes.
   */
  onSelectSession: (sessionId: string, transcript: ChatMessage[]) => void;
}

export function ChatHistoryDrawer({ deviceId, projectKey, mode = "customer", anonToken, onNewSession, refreshToken = 0, onSelectSession }: ChatHistoryDrawerProps) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<Array<ChatSessionSummary | TrainingSessionSummary>>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  function openHistory(): void {
    setLoading(true);
    setError(null);
    setOpen(true);
  }

  useEffect(() => {
    if (!open || (mode !== "training" && !deviceId) || !projectKey) return;
    let cancelled = false;
    const loadSessions = mode === "training"
      ? fetchTrainingSessions(projectKey).then((items) => items as Array<ChatSessionSummary | TrainingSessionSummary>)
      : fetchChatSessions(deviceId, projectKey, anonToken).then((items) =>
        items.filter((item) => item.project_key === projectKey) as Array<ChatSessionSummary | TrainingSessionSummary>
      );
    void loadSessions.then((items) => {
      if (!cancelled) setSessions(items);
    }).catch(() => {
      if (!cancelled) {
        setSessions([]);
        setError("Không tải được lịch sử chat. Anh/chị vui lòng thử lại.");
      }
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [anonToken, deviceId, mode, open, projectKey, refreshToken, reloadKey]);

  async function selectSession(session: ChatSessionSummary | TrainingSessionSummary): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      let transcript;
      let selectedSessionId: string;
      if (mode === "training") {
        const trainingSession = session as TrainingSessionSummary;
        const detail = await fetchTrainingSession(trainingSession.id);
        if (detail.context.project_key !== projectKey) throw new Error("Training session project mismatch");
        transcript = detail.messages;
        selectedSessionId = trainingSession.id;
      } else {
        selectedSessionId = (session as ChatSessionSummary).session_id;
        transcript = await fetchChatSessionMessages(deviceId, selectedSessionId, projectKey, anonToken);
      }
      // The drawer closes on selection: ChatPage owns hydration and the URL
      // from here on. A fetch failure keeps the list open with the inline
      // retry alert so the user can try again without losing context.
      onSelectSession(selectedSessionId, transcript.map((item, index) => ({
        id: `${selectedSessionId}-${index}`,
        role: item.role,
        content: item.content,
        ...(item.meta && typeof item.meta === "object" ? item.meta : {}),
      })));
      setOpen(false);
    } catch {
      setError("Không mở được đoạn chat này. Anh/chị vui lòng thử lại.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <Button icon={<HistoryOutlined />} onClick={openHistory} aria-label="Lịch sử chat">
        Lịch sử chat
      </Button>
      <Drawer title="Lịch sử chat" open={open} onClose={() => setOpen(false)} width={420}>
        {error ? (
          <Alert
            type="error"
            showIcon
            message={error}
            action={<Button onClick={() => { setError(null); setReloadKey((key) => key + 1); }}>Thử lại</Button>}
          />
        ) : loading ? <Spin /> : sessions.length === 0 ? <Empty description="Chưa có đoạn chat nào" /> : (
          <List dataSource={sessions} renderItem={(session) => (
            <List.Item actions={[<Button key="open" type="link" onClick={() => void selectSession(session)}>Mở</Button>]}>
              <List.Item.Meta
                title={session.title || "Đoạn chat"}
                description={`${session.message_count} tin nhắn`}
              />
              {"handed_off" in session && session.handed_off && <Tag color="green">Đã để lại SĐT</Tag>}
            </List.Item>
          )} />
        )}
        <Button type="primary" block onClick={() => { setOpen(false); onNewSession(); }} style={{ marginTop: 16 }}>
          Đoạn chat mới
        </Button>
      </Drawer>
    </>
  );
}
