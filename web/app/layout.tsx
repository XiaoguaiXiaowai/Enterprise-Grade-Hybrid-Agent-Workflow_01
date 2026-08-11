import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "企业级 Agent Harness Demo",
  description: "企业 IT 工单智能处理系统",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh">
      <body>{children}</body>
    </html>
  );
}
