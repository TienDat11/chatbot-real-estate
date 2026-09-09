"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { App as AntApp, Button, Spin, Typography } from "antd";
import { fetchProjectCatalog, type ProjectSummary } from "@/lib/projectCatalog";
import { decideBareRootRedirect, projectPath } from "@/lib/projectRoute";
import { getStoredProjectKey, storeProjectKey } from "@/features/chat/identity";
import { shouldForceProjectPicker, type ActiveProject } from "@/features/chat/activeProjects";
import { ProjectPicker } from "@/features/chat/ProjectPicker";
import { useOptionalAuth } from "@/lib/AuthProvider";
import { C } from "@/lib/tokens";

/**
 * Maps the router's catalogue row (display_name) onto the picker's ActiveProject
 * shape (name), so the existing ProjectPicker renders the same copy the routed
 * ChatPage uses. Optional fields stay optional so a row missing a short name or
 * geo data still lists cleanly.
 */
function toActiveProject(project: ProjectSummary): ActiveProject {
  return {
    project_key: project.project_key,
    name: project.display_name,
    short_name: project.short_name,
    location: project.location,
    lat: project.lat,
    lng: project.lng,
    is_hot: project.is_hot,
  };
}

/** Reads the stored project choice; private-mode storage counts as no choice. */
function readStoredProjectKey(): string | null {
  try {
    return getStoredProjectKey(window.localStorage);
  } catch {
    // Storage unavailable (private mode): no remembered choice, force the picker.
    return null;
  }
}

/**
 * Login link without stale query params (residual audit): the bare root's
 * search string only ever carries legacy/gate bookkeeping (?project_key=,
 * picker state), which must not ride into `next` and round-trip an obsolete
 * project or session reference after sign-in. The path is enough.
 */
function loginHrefForCurrentPath(): string {
  return `/login?next=${encodeURIComponent(window.location.pathname)}`;
}

/**
 * Bare "/" is a gate, not a chat page. Wave-1 UX rule: a first visit with more
 * than one active project MUST show the ProjectPicker (forced, no escape) —
 * never a silent redirect to a default project. A previously stored choice
 * (ragre.project_key, written only by the picker) and legacy ?project_key=
 * links are explicit user intents and still navigate straight to their project.
 * If the catalogue stays unreachable after retries we show a recoverable error
 * state with an explicit retry.
 */
export function RootProjectGate() {
  const router = useRouter();
  const { message } = AntApp.useApp();
  const auth = useOptionalAuth();
  const [catalog, setCatalog] = useState<ProjectSummary[] | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  const [failed, setFailed] = useState(false);

  const resolve = useCallback((): Promise<void> => {
    setFailed(false);
    setCatalog(null);
    setShowPicker(false);
    return fetchProjectCatalog()
      .then((projects) => {
        setCatalog(projects);
        // Legacy ?project_key=<key> is an explicit link choice: convert it to
        // path form regardless of any stored key so stale links keep working.
        const legacyKey = new URLSearchParams(window.location.search).get("project_key");
        if (legacyKey !== null && legacyKey.length > 0) {
          const decision = decideBareRootRedirect(projects, window.location.search);
          if (decision.kind === "legacy_unknown") {
            message.warning("Dự án theo liên kết cũ không còn hoạt động. Đã chuyển sang dự án mặc định.");
          }
          router.replace(decision.kind === "redirect" ? decision.path : decision.fallbackPath);
          return;
        }
        const storedKey = readStoredProjectKey();
        // Master-plan rule (story 10.1): more than one active project and no
        // explicit stored choice -> force the picker. A stored key is a user
        // decision, never a system default, so it skips the gate.
        if (shouldForceProjectPicker(projects.length, storedKey)) {
          setShowPicker(true);
          return;
        }
        // A stored choice that is still active is honoured as-is.
        if (storedKey !== null && projects.some((p) => p.project_key === storedKey)) {
          router.replace(projectPath(storedKey));
          return;
        }
        // No valid stored choice. A stale key (project closed) must never
        // silently default to a guessed project: with more than one active
        // project the picker asks again.
        if (projects.length > 1) {
          setShowPicker(true);
          return;
        }
        // A single active project is the only scope there is.
        router.replace(projectPath(projects[0].project_key));
      })
      .catch(() => {
        // Every retry failed: recoverable dead end with an explicit retry —
        // never a crash, never a guessed default project.
        setFailed(true);
        message.error("Không tải được danh sách dự án. Vui lòng thử lại.");
      });
  }, [router, message]);

  useEffect(() => {
    const resolveId = window.setTimeout(() => void resolve(), 0);
    return () => window.clearTimeout(resolveId);
  }, [resolve]);

  const handleSelect = useCallback(
    (key: string) => {
      try {
        storeProjectKey(window.localStorage, key);
      } catch {
        // Storage unavailable (private mode): the choice still applies for this visit.
      }
      router.replace(projectPath(key));
    },
    [router]
  );

  if (failed) {
    return (
      <div
        className="app-viewport-height"
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 16,
          padding: 24,
        }}
      >
        <Typography.Text strong style={{ fontSize: 16 }}>
          Không tải được danh sách dự án
        </Typography.Text>
        <Typography.Text type="secondary" style={{ textAlign: "center" }}>
          Máy chủ đang không phản hồi. Anh/chị vui lòng thử lại sau ít phút.
        </Typography.Text>
        <Button type="primary" size="large" onClick={() => void resolve()}>
          Thử lại
        </Button>
      </div>
    );
  }

  if (catalog === null || !showPicker) {
    // Loading, or a redirect already dispatched (stored choice / single active
    // project / legacy link): keep the neutral frame until navigation unmounts us.
    return (
      <div
        className="app-viewport-height"
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 16,
          padding: 24,
        }}
      >
        <Spin size="large" />
        <Typography.Text type="secondary">Đang mở danh sách dự án...</Typography.Text>
        {auth?.user ? (
          <Button type="link" href={loginHrefForCurrentPath()}>
            Mở trang đăng nhập lại
          </Button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="app-viewport-height" style={{ background: C.bg }}>
      <ProjectPicker
        open
        force
        projects={catalog.map(toActiveProject)}
        currentProjectKey={null}
        onSelect={handleSelect}
        onClose={() => undefined}
        loginHref={loginHrefForCurrentPath()}
      />
    </div>
  );
}
