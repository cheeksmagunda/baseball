import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ben Oracle",
  description: "Sees what others miss. AI-powered lineup optimizer for Real Sports DFS.",
  openGraph: {
    title: "Ben Oracle",
    description: "Sees what others miss.",
    siteName: "Ben Oracle",
  },
  applicationName: "Ben Oracle",
  appleWebApp: {
    title: "Ben Oracle",
    capable: true,
    statusBarStyle: "black-translucent",
  },
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
