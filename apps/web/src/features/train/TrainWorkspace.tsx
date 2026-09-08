"use client";

import { ChatPage } from "@/components/ChatPage";

/**
 * Internal Q&A chat for sales onboarding (story 11.2 / ISSUE-12).
 *
 * This is now a thin wrapper over the shared ChatPage shell (same presentation,
 * transport and state machinery as the customer chat) rendered in explicit
 * `mode="training"`: queries carry answer_mode="training" with selected
 * project context metadata, the Firebase bearer provider stays installed for
 * signed-in sales users, and every customer selling surface (map, project
 * picker, lead form, phone CTA, quota badge/wall) is absent. The training
 * history drawer IS present and scoped to the selected project. A `sessionId`
 * deep link (from the route's ?sessionId=) hydrates a saved training transcript
 * exactly like the customer project route does. Server auth remains
 * authoritative via the route gate in app/train/page.tsx.
 */
export function TrainWorkspace({ sessionId }: { sessionId?: string }) {
  return <ChatPage mode="training" shellOwned sessionId={sessionId} />;
}
