import { Tag } from "antd";
import type { Confidence } from "@rag-ragre/contracts";

/** 3-tier confidence colors (AD-6 L4): HIGH / MEDIUM / LOW. */
export const CONFIDENCE_STYLE: Record<Confidence, { color: string; bg: string; label: string; tone: string }> = {
  HIGH: {
    color: "#16A34A",
    bg: "#EAF7EF",
    label: "Độ tin cậy cao",
    tone: "emerald",
  },
  MEDIUM: {
    color: "#D97706",
    bg: "#FDF3E3",
    label: "Độ tin cậy trung bình",
    tone: "amber",
  },
  LOW: {
    color: "#DC2626",
    bg: "#FDECEC",
    label: "Độ tin cậy thấp",
    tone: "red",
  },
};

export interface ConfidenceBadgeProps {
  confidence: Confidence;
  /** Show a text label instead of just the color. */
  showLabel?: boolean;
}

/** 3-tier confidence badge — HIGH green, MEDIUM amber, LOW red. */
export function ConfidenceBadge({ confidence, showLabel = true }: ConfidenceBadgeProps) {
  const style = CONFIDENCE_STYLE[confidence];
  return (
    <Tag
      data-testid="confidence-badge"
      style={{
        color: style.color,
        backgroundColor: style.bg,
        borderColor: `${style.color}33`,
        borderRadius: 999,
        fontWeight: 600,
        fontSize: 12,
        paddingInline: 10,
        lineHeight: "20px",
        marginInlineEnd: 0,
      }}
    >
      {showLabel ? style.label : confidence}
    </Tag>
  );
}
