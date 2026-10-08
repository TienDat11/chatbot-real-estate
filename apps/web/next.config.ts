import type { NextConfig } from "next";

function getConfiguredMediaPatterns(): Array<{ protocol: "https"; hostname: string; pathname: string }> {
  const raw = process.env.NEXT_PUBLIC_MEDIA_ORIGINS_JSON;
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const value = item as { origin?: unknown; pathPrefixes?: unknown };
      if (typeof value.origin !== "string" || !Array.isArray(value.pathPrefixes)) return [];
      const origin = value.origin.trim().replace(/\/$/, "");
      let url: URL;
      try {
        url = new URL(origin);
      } catch {
        return [];
      }
      if (url.protocol !== "https:" || url.username || url.password || url.port) return [];
      const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
      const isPrivateIpv4 = (address: string): boolean =>
        /^(?:10|127|169\.254|192\.168)\./.test(address) || /^172\.(?:1[6-9]|2\d|3[0-1])\./.test(address);
      const mappedIpv4 = hostname.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/)?.[1];
      const isPrivateIpv6 =
        hostname === "::" || hostname === "::1" ||
        (mappedIpv4 !== undefined && isPrivateIpv4(mappedIpv4)) ||
        /^fe[89ab][0-9a-f]{1,2}:/.test(hostname) || /^f[cd][0-9a-f]{2}:/.test(hostname);
      if (hostname === "localhost" || hostname.endsWith(".local") || hostname === "0.0.0.0" || isPrivateIpv4(hostname) || isPrivateIpv6) return [];
      const pathPrefixes = value.pathPrefixes
        .filter((prefix): prefix is string => typeof prefix === "string" && prefix.length > 0)
        .map((prefix) => (prefix.trim().startsWith("/") ? prefix.trim() : `/${prefix.trim()}`))
        .filter((prefix) => prefix !== "/" && !prefix.includes("..") && prefix.endsWith("/"));
      return pathPrefixes.map((prefix) => ({ protocol: "https" as const, hostname, pathname: `${prefix.replace(/\/$/, "")}/**` }));
    });
  } catch {
    return [];
  }
}
// Workspace packages ship unbundled source; let Next transpile them directly.
const TRANSPILE_PACKAGES = [
  "@rag-ragre/contracts",
  "@rag-ragre/ui",
  "antd",
  "@ant-design/icons",
];

// FastAPI dev server runs on :8000; the production API origin comes from env.
// Set NEXT_PUBLIC_API_PROXY_TARGET=/ to serve the API from the same origin
// (no rewrite is applied in that case). The fallback mirrors
// DEFAULT_API_PROXY_TARGET in @rag-ragre/contracts (not importable here:
// next.config loads via Node ESM, which requires explicit file extensions).
const apiProxyTarget =
  process.env.NEXT_PUBLIC_API_PROXY_TARGET ?? "http://localhost:8000";

// FastAPI's route prefixes are inconsistent: lead (lead.py) and sales
// (sales.py) mount under /api, while query, llms-hello, health and ready are
// declared directly on the app root with no /api prefix. No single rewrite
// rule can serve both groups, so each client path whose backend twin lacks
// the prefix is mapped per-route. These specific rules must come first:
// Next matches rewrites in array order and stops at the first hit, so a
// catch-all /api/:path* placed ahead of them would swallow /api/query and
// rewrite it to /api/query on the backend, which 404s.
const apiRewrites =
  apiProxyTarget === "/" || apiProxyTarget === ""
    ? []
    : [
        // Chat SSE stream: POST /api/query -> FastAPI /query (no prefix).
        {
          source: "/api/query",
          destination: `${apiProxyTarget}/query`,
        },
        // Greeting endpoint: /api/llms-hello -> FastAPI /llms-hello (no prefix).
        {
          source: "/api/llms-hello",
          destination: `${apiProxyTarget}/llms-hello`,
        },
        // Keep the /api prefix for everything else: /api/lead, /api/sales/*.
        {
          source: "/api/:path*",
          destination: `${apiProxyTarget}/api/:path*`,
        },
      ];

const nextConfig: NextConfig = {
  transpilePackages: TRANSPILE_PACKAGES,
  rewrites: async () => apiRewrites,
  // Disable the server's response compression. The Next dev/prod server would
  // otherwise gzip-buffer the SSE stream proxied from FastAPI, which breaks
  // incremental token delivery (ERR_INCOMPLETE_CHUNKED_ENCODING). FastAPI
  // streams are already chunked; we only need them passed through verbatim.
  images: {
    remotePatterns: getConfiguredMediaPatterns(),
  },
  compress: false,
  experimental: {
    // The dev proxy kills an idle proxied connection after 30s by default
    // (dist/server/lib/router-utils/proxy-request.js). A cold LightRAG query
    // can stay silent for 40s+ before the first SSE token, so the proxy was
    // severing POST /api/query mid-stream (ERR_INCOMPLETE_CHUNKED_ENCODING)
    // before the `done` frame. 300s keeps slow first queries alive.
    proxyTimeout: 300000,
  },
};

export default nextConfig;
