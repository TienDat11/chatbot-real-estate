import type { Metadata, Viewport } from "next";
import { Be_Vietnam_Pro } from "next/font/google";
import { AntdRegistry } from "@ant-design/nextjs-registry";
import { ProThemeProvider } from "@/components/ProThemeProvider";
import "./globals.css";

const beVietnam = Be_Vietnam_Pro({
  subsets: ["latin", "vietnamese"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-be-vietnam",
  display: "swap",
});

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
    <html lang="vi" className={`${beVietnam.variable} h-full antialiased`}>
      <body className="min-h-full" style={{ background: "#FAF7F2" }}>
        <AntdRegistry>
          <ProThemeProvider>{children}</ProThemeProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
