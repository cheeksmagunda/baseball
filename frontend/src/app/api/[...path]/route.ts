import { NextRequest, NextResponse } from "next/server";

function getBackendUrl(): string {
  const raw = process.env.API_URL ?? process.env.BACKEND_URL ?? "http://localhost:8000";
  const url = raw.startsWith("http") ? raw : `http://${raw}`;
  return url.replace(/\/+$/, "");
}

async function proxy(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const target = `${getBackendUrl()}${pathname}${search}`;

  const headers: Record<string, string> = { "content-type": "application/json" };

  const init: RequestInit = {
    method: req.method,
    headers,
  };

  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }

  try {
    const upstream = await fetch(target, init);
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown proxy error";
    return NextResponse.json(
      { detail: `Backend unreachable: ${message}`, target },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
export const PATCH = proxy;
