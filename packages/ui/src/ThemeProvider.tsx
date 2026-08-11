import type { ReactNode } from "react";
import { ConfigProvider } from "antd";
import viVN from "antd/locale/vi_VN";
import dayjs from "dayjs";
import "dayjs/locale/vi";

dayjs.locale("vi");

/**
 * Design tokens enterprise-trust (xanh navy #1F46A8) cho toàn bộ app.
 * Bọc toàn bộ app trong <ThemeProvider>; mọi component dưới đây đều
 * kế thừa token qua ConfigProvider context.
 */
export interface ThemeProviderProps {
  children: ReactNode;
}

export const DESIGN_TOKENS = {
  colorPrimary: "#1F46A8",
  colorInfo: "#1F46A8",
  colorBgLayout: "#F7F8FA",
  colorTextBase: "#1A2233",
  colorTextSecondary: "#5B6478",
  colorBorderSecondary: "#E9ECF2",
  borderRadius: 10,
  borderRadiusLG: 14,
  fontFamily:
    "'Be Vietnam Pro', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
} as const;

export function ThemeProvider({ children }: ThemeProviderProps) {
  return (
    <ConfigProvider
      locale={viVN}
      theme={{
        token: DESIGN_TOKENS,
        components: {
          Table: {
            headerBg: "#F4F6FA",
            headerColor: "#1A2233",
            rowHoverBg: "#F7F9FD",
          },
          Button: {
            fontWeight: 600,
          },
        },
      }}
    >
      {children}
    </ConfigProvider>
  );
}
