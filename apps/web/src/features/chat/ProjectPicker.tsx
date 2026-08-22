"use client";

import { Button, Modal } from "antd";
import { ApartmentOutlined, CheckOutlined } from "@ant-design/icons";
import { C, FS, RADIUS } from "@/lib/tokens";
import type { ActiveProject } from "./activeProjects";

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
}

/**
 * Multi-project chooser shown when the backend answers 422 PROJECT_SCOPE:
 * more than one project is active and none was chosen. Senior-first: 17px
 * labels, 48px touch targets, one navy accent, explicit per-project copy so a
 * caller never has to guess what project a question was scoped to.
 */
export function ProjectPicker({
  open,
  projects,
  currentProjectKey,
  onSelect,
  onClose,
}: ProjectPickerProps) {
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      centered
      width={520}
      keyboard
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
        {projects.map((project) => {
          const selected = project.project_key === currentProjectKey;
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
                padding: "10px 16px",
                textAlign: "left",
                fontFamily: "inherit",
                cursor: "pointer",
                background: selected ? C.primarySoft : C.surface,
                border: "2px solid " + (selected ? C.primary : C.borderStrong),
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
                  background: selected ? C.primary : C.surfaceAlt,
                  color: selected ? "#fff" : C.primary,
                  fontSize: 18,
                }}
              >
                <ApartmentOutlined />
              </span>
              <span style={{ flex: 1, minWidth: 0 }}>
                <span
                  style={{
                    display: "block",
                    fontSize: FS.body,
                    fontWeight: 700,
                    lineHeight: "24px",
                    color: C.text,
                  }}
                >
                  {project.ten_thuong_mai}
                </span>
                {(project.vi_tri || project.ten_phap_ly) && (
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
                    {project.vi_tri ?? project.ten_phap_ly}
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
    </Modal>
  );
}
