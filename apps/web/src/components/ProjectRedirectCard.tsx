"use client";

import Link from "next/link";
import { Button } from "antd";
import type { ProjectRedirect } from "@/lib/projectRedirect";
import { C, RADIUS } from "@/lib/tokens";
import { canonicalProjectUrl } from "@/lib/canonicalProjectUrl";
import { SESSION_KEY } from "@/features/chat/identity";

interface ProjectRedirectCardProps {
  redirect: ProjectRedirect;
}

/**
 * Prominent call-to-action under an answer that actually belongs to another
 * project (cross-project guardrail). A real <Link> (not onClick) keeps
 * middle-click/copy-link working and lands on /project/<key>, where the routed
 * ChatPage applies the same switch reset as the picker. Only mounted when the
 * message carries a normalized redirect, so absence renders nothing.
 */
function safeStoredSessionId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

export function ProjectRedirectCard({ redirect }: ProjectRedirectCardProps) {
  // Compact name when available ("The Soleil"), full display name otherwise.
  const label = redirect.shortName ?? redirect.displayName;
  const href = canonicalProjectUrl(redirect.project_key, safeStoredSessionId());
  return (
    <div
      style={{
        marginTop: 12,
        padding: 12,
        background: C.goldSoft,
        border: `1px solid ${C.goldBorder}`,
        borderRadius: RADIUS.card,
      }}
    >
      <Link
        href={href}
        aria-label={`Chuyển sang dự án ${label}`}
        style={{ display: "block" }}
      >
        <Button type="primary" block size="large" style={{ fontWeight: 600 }}>
          Chuyển sang dự án {label} →
        </Button>
      </Link>
    </div>
  );
}
