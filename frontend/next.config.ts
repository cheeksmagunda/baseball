import type { NextConfig } from "next";

function getBackendUrl(): string {
  const raw = process.env.API_URL ?? "http://localhost:8000";
  const url = raw.startsWith("http") ? raw : `http://${raw}`;
  return url.replace(/\/+$/, "");
}

const nextConfig: NextConfig = {
  output: "standalone",
  rewrites: async () => [
    {
      source: "/api/:path*",
      destination: `${getBackendUrl()}/api/:path*`,
    },
  ],
};

export default nextConfig;
