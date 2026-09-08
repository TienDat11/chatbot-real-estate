import type { Metadata, Viewport } from "next";
import { AntdRegistry } from "@ant-design/nextjs-registry";
import { ProThemeProvider } from "@/components/ProThemeProvider";
import { ClientNotificationShell } from "@/components/ClientNotificationShell";
// Self-hosted Be Vietnam Pro (fontsource): the server cannot reach
// fonts.googleapis.com at runtime, so next/font/google warned and fell back.
// Each weight css carries all subsets (latin + vietnamese) via unicode-range.
import "@fontsource/be-vietnam-pro/400.css";
import "@fontsource/be-vietnam-pro/500.css";
import "@fontsource/be-vietnam-pro/600.css";
import "@fontsource/be-vietnam-pro/700.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "RAG Real Estate — Tra cứu pháp lý bất động sản",
  description:
    "Trợ lý tra cứu văn bản pháp luật, quy hoạch và hồ sơ dự án bất động sản cho nhân viên mua giới.",
};

export const viewport: Viewport = {
  themeColor: "#0E2A47",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="vi" className="h-full antialiased">
      <body className="min-h-full">
        <AntdRegistry>
          <ProThemeProvider><ClientNotificationShell>{children}</ClientNotificationShell></ProThemeProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
