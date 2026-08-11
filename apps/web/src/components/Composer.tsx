"use client";

import { Button, Input } from "antd";
import { SendOutlined } from "@ant-design/icons";
import {
  COMPOSER_PLACEHOLDER_IDLE,
  COMPOSER_PLACEHOLDER_STREAMING,
} from "@/lib/constants";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  disabled: boolean;
  streaming: boolean;
}

/**
 * Question input: Enter sends, Shift+Enter inserts a newline.
 * Send is disabled while streaming or when the input is empty.
 */
export function Composer({ value, onChange, onSend, disabled, streaming }: ComposerProps) {
  const canSend = !disabled && !streaming && value.trim().length > 0;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend) onSend();
    }
  };

  return (
    <div
      style={{
        maxWidth: 860,
        margin: "0 auto",
        display: "flex",
        gap: 10,
        alignItems: "flex-end",
      }}
    >
      <Input.TextArea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={streaming ? COMPOSER_PLACEHOLDER_STREAMING : COMPOSER_PLACEHOLDER_IDLE}
        autoSize={{ minRows: 1, maxRows: 5 }}
        disabled={disabled}
        style={{
          borderRadius: 12,
          padding: "10px 14px",
          fontSize: 14,
          resize: "none",
          borderColor: "#D5DBE6",
          background: "#FFFFFF",
        }}
        aria-label="Câu hỏi"
      />
      <Button
        type="primary"
        icon={<SendOutlined />}
        onClick={onSend}
        disabled={!canSend}
        loading={streaming}
        style={{ borderRadius: 12, height: 42, minWidth: 92 }}
      >
        Gửi
      </Button>
    </div>
  );
}
