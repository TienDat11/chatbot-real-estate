"use client";

import { CheckOutlined, DownOutlined, UpOutlined } from "@ant-design/icons";
import { Button } from "antd";
import { useId, useState } from "react";

const STEPS = [
  "Hiểu yêu cầu",
  "Tìm nguồn phù hợp",
  "Kiểm tra thông tin",
  "Soạn câu trả lời",
] as const;

interface ProgressStepsProps {
  /** 0-based index of the step currently running. Invalid values are clamped. */
  activeStep: number;
}

export function ProgressSteps({ activeStep }: ProgressStepsProps) {
  const numericStep = Number.isFinite(activeStep) ? Math.trunc(activeStep) : 0;
  const safeStep = Math.max(0, Math.min(numericStep, STEPS.length - 1));
  const [expanded, setExpanded] = useState(false);
  const timelineId = `response-progress-timeline-${useId().replace(/:/g, "")}`;
  const currentLabel = `Đang ${STEPS[safeStep].toLocaleLowerCase()}`;


  return (
    <section className="response-progress" aria-label="Tiến trình trả lời">
      <div className="response-progress__header">
        <div className="response-progress__status" aria-live="polite">
          <span className="response-progress__spinner" aria-hidden="true"><span className="step-spinner" /></span>
          <span>{currentLabel}</span>
        </div>
        <Button
          type="text"
          shape="circle"
          className="response-progress__toggle"
          aria-label={expanded ? "Thu gọn tiến trình" : "Mở tiến trình"}
          aria-expanded={expanded}
          aria-controls={timelineId}
          onClick={() => setExpanded((value) => !value)}
          icon={expanded ? <UpOutlined aria-hidden="true" /> : <DownOutlined aria-hidden="true" />}
        />
      </div>
      {expanded && (
        <ol id={timelineId} className="response-progress__timeline">
          {STEPS.slice(0, safeStep + 1).map((label, index) => {
            const done = index < safeStep;
            return (
              <li key={label} className={`response-progress__item${done ? " is-complete" : " is-active"}`}>
                <span className="response-progress__marker" aria-hidden="true">
                  {done ? <CheckOutlined /> : <span className="step-spinner" />}
                </span>
                <span>{label}</span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
