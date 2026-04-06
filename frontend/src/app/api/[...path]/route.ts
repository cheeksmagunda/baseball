import { NextRequest, NextResponse } from "next/server";

function getBackendUrl(): string {
  const raw = process.env.API_URL ?? process.env.BACKEND_URL ?? "http://localhost:8000";
  const url = raw.startsWith("http") ? raw : `http://${raw}`;
  return url.replace(/\/+$/, "");
}

// Follow redirects manually so POST/PUT/PATCH are never silently downgraded to
// GET on 301/302.  Node's built-in fetch follows those redirects per the HTTP
// spec but changes the method to GET, which causes a 405 from FastAPI when
// (for example) Railway performs an HTTP→HTTPS redirect.
async function fetchWithMethodPreservingRedirects(
  url: string,
  init: RequestInit,
  maxRedirects = 5,
): Promise<Response> {
  let currentUrl = url;
  for (let i = 0; i < maxRedirects; i++) {
    const resp = await fetch(currentUrl, { ...init, redirect: "manual" });
    const status = resp.status;
    // 301/302/303/307/308 — follow while preserving method & body
    if (status >= 300 && status < 400) {
      const location = resp.headers.get("location");
      if (!location) return resp;
      currentUrl = location.startsWith("http")
        ? location
        : new URL(location, currentUrl).href;
      continue;
    }
    return resp;
  }
  // Exceeded max redirects — one final attempt without redirect override
  return fetch(currentUrl, init);
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
    const upstream = await fetchWithMethodPreservingRedirects(target, init);
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
export const HEAD = proxy;
