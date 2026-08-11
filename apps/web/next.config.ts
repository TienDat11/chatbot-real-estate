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

const apiRewrites =
  apiProxyTarget === "/" || apiProxyTarget === ""
    ? []
    : [
        {
          source: "/api/:path*",
          destination: `${apiProxyTarget}/:path*`,
        },
      ];

const nextConfig: NextConfig = {
  transpilePackages: TRANSPILE_PACKAGES,
  rewrites: async () => apiRewrites,
};

export default nextConfig;
