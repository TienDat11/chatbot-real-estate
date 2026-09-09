"use client";

/**
 * Premium antd theme for the web app.
 *
 * Mirrors the enterprise tokens @rag-ragre/ui normally provides but lives in
 * apps/web so shared routes use the same bright surfaces, navy actions, and
 * accessible control affordances without touching the shared package.
 */
import type { ReactNode } from "react";
// Hoisted once at the shared provider boundary so every Ant Design surface,
// including routes that render outside ChatPage, receives React 19 support.
import "@ant-design/v5-patch-for-react-19";
import { ConfigProvider } from "antd";
import viVN from "antd/locale/vi_VN";
import dayjs from "dayjs";
import "dayjs/locale/vi";
import { C, CONTROL_BORDER, CONTROL_TEXT } from "@/lib/tokens";

dayjs.locale("vi");

export interface ProThemeProviderProps {
  children: ReactNode;
}

export function ProThemeProvider({ children }: ProThemeProviderProps) {
  return (
    <ConfigProvider
      locale={viVN}
      theme={{
        token: {
          colorPrimary: C.primary,
          colorInfo: C.primary,
          colorBgLayout: C.bg,
          colorBgContainer: C.surface,
          colorTextBase: C.text,
          colorTextSecondary: C.textMuted,
          // R6 / FR-21: antd's default placeholder (rgba black 25%) and
          // disabled tiers fall below 4.5:1 on bright surfaces; the control
          // tokens are verified by tokens.contrast.test.ts.
          colorTextPlaceholder: CONTROL_TEXT.placeholder,
          colorTextQuaternary: CONTROL_TEXT.quaternary,
          colorTextDisabled: CONTROL_TEXT.disabled,
          colorBorder: C.borderStrong,
          colorBorderSecondary: C.border,
          fontSize: 17,
          controlHeight: 40,
          controlHeightLG: 44,
          controlPaddingHorizontal: 16,
          borderRadius: 12,
          borderRadiusLG: 16,
          fontFamily:
            "'Be Vietnam Pro', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        },
        components: {
          Table: {
            headerBg: C.surfaceAlt,
            headerColor: C.text,
            rowHoverBg: "#FAF6F0",
          },
          Button: {
            fontWeight: 600,
            colorTextDisabled: CONTROL_TEXT.disabled,
            colorBgContainerDisabled: C.surfaceAlt,
          },
          // Keep control boundaries perceivable: hover border >= 3:1
          // (non-text) on light backgrounds, focus ring stays navy.
          // TextArea renders through the same .ant-input classes as Input,
          // so a single Input override covers both.
          Input: {
            activeBorderColor: C.primary,
            hoverBorderColor: CONTROL_BORDER.hover,
            activeShadow:
              "0 0 0 2px rgba(14, 42, 67, 0.12)",
          },
          Select: {
            optionSelectedBg: C.primarySoft,
          },
          Tabs: {
            inkBarColor: C.gold,
            itemSelectedColor: C.primary,
            itemHoverColor: C.primaryHover,
          },
          Modal: {
            borderRadiusLG: 20,
          },
        },
      }}
    >
      {children}
    </ConfigProvider>
  );
}
