"use client";

/**
 * Premium antd theme for the web app.
 *
 * Mirrors the enterprise tokens @rag-ragre/ui normally provides but lives in
 * apps/web so the sales-demo redesign can tune antd surfaces (deep navy primary,
 * warm cream layout, gold ink) without touching the shared package. Same viVN
 * locale + dayjs wiring so behavior is identical to the shared provider.
 */
import type { ReactNode } from "react";
import { ConfigProvider } from "antd";
import viVN from "antd/locale/vi_VN";
import dayjs from "dayjs";
import "dayjs/locale/vi";
import { C } from "@/lib/tokens";

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
          colorTextBase: C.text,
          colorTextSecondary: C.textMuted,
          colorBorderSecondary: C.border,
          fontSize: 17,
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
