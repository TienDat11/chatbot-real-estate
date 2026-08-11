import type { Metadata, Viewport } from "next";
import { Be_Vietnam_Pro } from "next/font/google";
import { AntdRegistry } from "@ant-design/nextjs-registry";
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
  themeColor: "#1F46A8",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="vi" className={`${beVietnam.variable} h-full antialiased`}>
      <body className="min-h-full" style={{ background: "#F7F8FA" }}>
        <AntdRegistry>{children}</AntdRegistry>
      </body>
    </html>
  );
}
