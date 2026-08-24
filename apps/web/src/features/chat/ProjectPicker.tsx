"use client";

import { useMemo } from "react";
import { Button, Modal } from "antd";
import { CheckOutlined, FireOutlined } from "@ant-design/icons";
import { C, FS, RADIUS } from "@/lib/tokens";
import { ProjectCover } from "@/components/ProjectCover";
import { useProjectCover } from "./projectMedia";
import {
  sortActiveProjects,
  projectDisplayLocation,
  projectDisplayName,
  type ActiveProject,
} from "./activeProjects";

// No width token exists in the design-token module (C/RADIUS/SHADOW/FS cover
// color, corner and type scales only), so the modal width stays a local named
// constant instead of a magic number (review n5). 680px fits the cover-on-left
// premium rows without truncating the full project address.
const PROJECT_PICKER_MODAL_WIDTH = 680;

export interface ProjectPickerProps {
  open: boolean;
  /** Active projects offered for selection (story 10.3). */
  projects: ActiveProject[];
  /** Currently selected project, when one is stored; highlighted in the list. */
  currentProjectKey?: string | null;
  /** Fired with the chosen key; the caller re-sends the pending query. */
  onSelect: (projectKey: string) => void;
  /** Dismiss without choosing; the pending question stays unanswered. */
  onClose: () => void;
  /**
   * Master-plan rule (story 10.1): when >1 project is active and none was
   * chosen the picker is FORCED — no "Để sau" escape and no mask/keyboard
   * dismissal, so the customer always picks before any query runs.
   */
  force?: boolean;
}

/**
 * Multi-project chooser. Shown (a) forced on load when >1 project is active
 * and no explicit choice is stored, and (b) when the backend answers 422
 * PROJECT_SCOPE after a question without a project. Each row is a premium
 * project card: real cover image from the backend hello media (gradient
 * fallback while loading), the display name (bold), the full location, and a
 * gold "Nổi bật" badge for hot projects; rows are sorted hot-first (Camellia
 * first), then by name. Senior-first: 17px labels, 48px touch targets, one
 * navy accent, explicit per-project copy so a caller never has to guess what
 * project a question was scoped to.
 */
export function ProjectPicker({
  open,
  projects,
  currentProjectKey,
  onSelect,
  onClose,
  force = false,
}: ProjectPickerProps) {
  const sorted = useMemo(() => sortActiveProjects(projects), [projects]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      centered
      width={PROJECT_PICKER_MODAL_WIDTH}
      keyboard={!force}
      maskClosable={!force}
      closable={!force}
      title={
        <span style={{ fontSize: 20, fontWeight: 700, color: C.text }}>
          Chọn dự án để được tư vấn
        </span>
      }
      styles={{ body: { paddingTop: 12 } }}
    >
      <p style={{ fontSize: 15, lineHeight: "24px", color: C.textMuted, margin: "0 0 16px" }}>
        Có nhiều dự án đang mở bán. Anh/chị vui lòng chọn dự án muốn tìm hiểu để câu trả lời
        được tư vấn đúng dự án.
      </p>
      <div role="listbox" aria-label="Danh sách dự án" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {sorted.map((project, idx) => (
          <ProjectCard
            key={project.project_key}
            project={project}
            selected={project.project_key === currentProjectKey}
            idx={idx}
            onSelect={onSelect}
          />
        ))}
      </div>
      {!force && (
        <Button
          block
          onClick={onClose}
          style={{
            marginTop: 16,
            height: 48,
            fontSize: 16,
            fontWeight: 600,
            borderRadius: RADIUS.btn,
            borderColor: C.borderStrong,
            color: C.textMuted,
          }}
        >
          Để sau
        </Button>
      )}
    </Modal>
  );
}

/** One premium project card: real cover image + name + location + hot badge. */
function ProjectCard({
  project,
  selected,
  idx,
  onSelect,
}: {
  project: ActiveProject;
  selected: boolean;
  idx: number;
  onSelect: (key: string) => void;
}) {
  const hot = project.is_hot === true;
  const name = projectDisplayName(project);
  const location = projectDisplayLocation(project) ?? project.ten_phap_ly;
  const cover = useProjectCover(project.project_key);

  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      onClick={() => onSelect(project.project_key)}
      className="project-card card-in"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        width: "100%",
        minHeight: 72,
        padding: 10,
        textAlign: "left",
        fontFamily: "inherit",
        cursor: "pointer",
        background: selected ? C.primarySoft : C.surface,
        border: "2px solid " + (selected ? C.gold : hot ? C.goldBorder : C.border),
        borderRadius: RADIUS.card,
        transition: "border-color .15s, box-shadow .15s, background .15s",
        animationDelay: `${idx * 40}ms`,
      }}
    >
      <span
        style={{
          flexShrink: 0,
          width: 96,
          height: 64,
          borderRadius: RADIUS.small,
          overflow: "hidden",
          display: "block",
        }}
        aria-hidden="true"
      >
        <ProjectCover src={cover} alt={name} ratio="3 / 2" warm={hot} />
      </span>
      <span style={{ flex: 1, minWidth: 0 }}>
        <span
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: FS.body,
            fontWeight: 700,
            lineHeight: "24px",
            color: C.text,
          }}
        >
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {name}
          </span>
          {hot && (
            <span
              role="status"
              className="chip-gold"
              style={{
                flexShrink: 0,
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                borderRadius: RADIUS.pill,
                padding: "1px 10px",
                fontSize: 12,
                fontWeight: 700,
                lineHeight: "20px",
              }}
            >
              <FireOutlined aria-hidden="true" style={{ fontSize: 12 }} />
              Nổi bật
            </span>
          )}
        </span>
        {location && (
          <span
            style={{
              display: "block",
              fontSize: 14,
              lineHeight: "22px",
              color: C.textMuted,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {location}
          </span>
        )}
      </span>
      {selected && (
        <span
          style={{
            flexShrink: 0,
            width: 26,
            height: 26,
            borderRadius: RADIUS.pill,
            background: C.gold,
            color: C.primary,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <CheckOutlined aria-hidden="true" style={{ fontSize: 14 }} />
        </span>
      )}
    </button>
  );
}
