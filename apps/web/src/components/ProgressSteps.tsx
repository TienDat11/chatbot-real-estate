"use client";

import { C } from "@/lib/tokens";

/**
 * ProgressSteps — 4-step vertical checklist shown while the assistant is
 * working (replaces the plain progress label). Each completed step gets a
 * green tick; the running step shows a small spinner. Senior-first, respects
 * prefers-reduced-motion.
 */

const STEPS = [
  "Hiểu câu hỏi",
  "Tra tài liệu",
  "Đối chiếu số liệu",
  "Soạn câu trả lời",
];

interface ProgressStepsProps {
  /** 0-based index of the step currently running. */
  activeStep: number;
}

export function ProgressSteps({ activeStep }: ProgressStepsProps) {
  return (
    <ol
      className="progress-steps"
      style={{
        listStyle: "none",
        margin: 0,
        padding: 0,
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      {STEPS.map((label, i) => {
        const done = i < activeStep;
        const running = i === activeStep;
        return (
          <li key={label} style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span
              aria-hidden="true"
              style={{
                width: 22,
                height: 22,
                borderRadius: "50%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 12,
                fontWeight: 700,
                background: done ? C.success : running ? C.primarySoft : C.surfaceAlt,
                color: done ? "#FFFFFF" : running ? C.primary : C.textGhost,
                border: running ? "2px solid " + C.primary : "1px solid " + C.border,
              }}
            >
              {done ? "✓" : running ? <span className="step-spinner" /> : i + 1}
            </span>
            <span
              style={{
                fontSize: 15,
                color: done || running ? C.text : C.textGhost,
                fontWeight: running ? 600 : 400,
              }}
            >
              {label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
