import type { NextConfig } from "next";

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
  compress: false,
};

export default nextConfig;
