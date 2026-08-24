"use client";

import { Button, Input } from "antd";
import { SendOutlined } from "@ant-design/icons";
import {
  COMPOSER_PLACEHOLDER_IDLE,
  COMPOSER_PLACEHOLDER_STREAMING,
} from "@/lib/constants";
import { C } from "@/lib/tokens";

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
      className="composer-shell"
      style={{
        maxWidth: 860,
        margin: "0 auto",
        display: "flex",
        gap: 10,
        alignItems: "flex-end",
        padding: 6,
        transition: "border-color 0.18s ease, box-shadow 0.18s ease",
      }}
    >
      <Input.TextArea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={streaming ? COMPOSER_PLACEHOLDER_STREAMING : COMPOSER_PLACEHOLDER_IDLE}
        autoSize={{ minRows: 1, maxRows: 5 }}
        disabled={disabled}
        variant="borderless"
        style={{
          padding: "10px 12px",
          fontSize: 15,
          lineHeight: "24px",
          resize: "none",
          background: "transparent",
          color: C.text,
        }}
        aria-label="Câu hỏi"
      />
      <Button
        type="primary"
        icon={<SendOutlined />}
        onClick={onSend}
        disabled={!canSend}
        loading={streaming}
        className="btn-terracotta"
        style={{
          borderRadius: 12,
          height: 42,
          minWidth: 92,
          border: "none",
          fontWeight: 600,
        }}
      >
        Gửi
      </Button>
    </div>
  );
}
