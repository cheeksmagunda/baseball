import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DFS Predictor — Real Sports Lineup Optimizer",
  description:
    "AI-powered dual-lineup optimizer for Real Sports DFS. Starting 5 and Moonshot lineups with trait-based scoring.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0a0b0f",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-surface-base text-text-primary">{children}</body>
    </html>
  );
}
