"use client";

import { useMemo } from "react";
import { Button, Modal } from "antd";
import { ApartmentOutlined, CheckOutlined, FireOutlined } from "@ant-design/icons";
import { C, FS, RADIUS } from "@/lib/tokens";
import {
  sortActiveProjects,
  projectDisplayLocation,
  projectDisplayName,
  type ActiveProject,
} from "./activeProjects";

// No width token exists in the design-token module (C/RADIUS/SHADOW/FS cover
// color, corner and type scales only), so the modal width stays a local named
// constant instead of a magic number (review n5). 560px keeps the two-line
// project rows (name + full location) readable without truncation.
const PROJECT_PICKER_MODAL_WIDTH = 560;

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
 * PROJECT_SCOPE after a question without a project. Each row shows the display
 * name (bold), the full location, and a "Nổi bật" badge for hot projects;
 * rows are sorted hot-first (Camellia first), then by name. Senior-first:
 * 17px labels, 48px touch targets, one navy accent, explicit per-project copy
 * so a caller never has to guess what project a question was scoped to.
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
      <div role="listbox" aria-label="Danh sách dự án" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {sorted.map((project) => {
          const selected = project.project_key === currentProjectKey;
          const hot = project.is_hot === true;
          const name = projectDisplayName(project);
          const location = projectDisplayLocation(project) ?? project.ten_phap_ly;
          return (
            <button
              key={project.project_key}
              type="button"
              role="option"
              aria-selected={selected}
              onClick={() => onSelect(project.project_key)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 14,
                width: "100%",
                minHeight: 56,
                padding: "12px 16px",
                textAlign: "left",
                fontFamily: "inherit",
                cursor: "pointer",
                background: selected ? C.primarySoft : hot ? C.warningSoft : C.surface,
                border: "2px solid " + (selected ? C.primary : hot ? C.warning : C.borderStrong),
                borderRadius: RADIUS.input,
                transition: "border-color .15s, background .15s",
              }}
            >
              <span
                aria-hidden="true"
                style={{
                  flexShrink: 0,
                  width: 40,
                  height: 40,
                  borderRadius: RADIUS.small,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  background: hot && !selected ? C.warningSoft : selected ? C.primary : C.surfaceAlt,
                  color: hot && !selected ? C.warning : selected ? "#fff" : C.primary,
                  fontSize: 18,
                }}
              >
                <ApartmentOutlined />
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
                      style={{
                        flexShrink: 0,
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                        background: C.warning,
                        color: "#fff",
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
                <CheckOutlined
                  aria-hidden="true"
                  style={{ flexShrink: 0, fontSize: 18, color: C.primary }}
                />
              )}
            </button>
          );
        })}
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
          }}
        >
          Để sau
        </Button>
      )}
    </Modal>
  );
}
