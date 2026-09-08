import type { Image as ImageContract, Video } from "@rag-ragre/contracts";

export interface PublicMediaOriginConfig {
  origin: string;
  pathPrefixes: string[];
}

function isPublicHttpsOrigin(origin: string): boolean {
  try {
    const url = new URL(origin);
    const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
    const isPrivateIpv4 = (address: string): boolean =>
      /^(?:10|127|169\.254|192\.168)\./.test(address) ||
      /^172\.(?:1[6-9]|2\d|3[0-1])\./.test(address);
    const mappedIpv4 = hostname.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/)?.[1];
    const isPrivateIpv6 =
      hostname === "::" ||
      hostname === "::1" ||
      (mappedIpv4 !== undefined && isPrivateIpv4(mappedIpv4)) ||
      /^fe[89ab][0-9a-f]{1,2}:/.test(hostname) ||
      /^f[cd][0-9a-f]{2}:/.test(hostname);

    if (url.protocol !== "https:" || url.username || url.password || url.port) return false;
    if (
      hostname === "localhost" ||
      hostname.endsWith(".local") ||
      hostname === "0.0.0.0" ||
      isPrivateIpv4(hostname) ||
      isPrivateIpv6
    ) {
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

function configuredOrigins(): PublicMediaOriginConfig[] {
  const raw = process.env.NEXT_PUBLIC_MEDIA_ORIGINS_JSON;
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const value = item as { origin?: unknown; pathPrefixes?: unknown };
      if (typeof value.origin !== "string" || !Array.isArray(value.pathPrefixes)) return [];
      const rawOrigin = value.origin.trim().replace(/\/$/, "");
      if (!isPublicHttpsOrigin(rawOrigin)) return [];
      const origin = new URL(rawOrigin).origin;
      const pathPrefixes = value.pathPrefixes
        .filter((prefix): prefix is string => typeof prefix === "string" && prefix.length > 0)
        .map((prefix) => {
          const normalized = prefix.trim();
          return normalized.startsWith("/") ? normalized : `/${normalized}`;
        })
        .filter((prefix) => prefix !== "/" && !prefix.includes("..") && prefix.endsWith("/"));
      return pathPrefixes.length > 0 ? [{ origin, pathPrefixes }] : [];
    });
  } catch {
    return [];
  }
}

export function isAllowedPublicMediaUrl(value: string | null | undefined): boolean {
  if (!value) return false;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return false;
    return configuredOrigins().some(
      ({ origin, pathPrefixes }) =>
        url.origin === origin && pathPrefixes.some((prefix) => url.pathname.startsWith(prefix)),
    );
  } catch {
    return false;
  }
}

export function filterPublicImages(images: ImageContract[] | undefined): ImageContract[] {
  return (Array.isArray(images) ? images : []).filter((image) => isAllowedPublicMediaUrl(image.url_cdn));
}

export function filterPublicVideos(videos: Video[] | undefined): Video[] {
  return (Array.isArray(videos) ? videos : []).filter(
    (video) => isAllowedPublicMediaUrl(video.url_cdn) && (!video.poster_url || isAllowedPublicMediaUrl(video.poster_url)),
  );
}

export function getConfiguredMediaPatterns(): Array<{ protocol: "https"; hostname: string; pathname: string }> {
  return configuredOrigins().flatMap(({ origin, pathPrefixes }) => {
    try {
      const url = new URL(origin);
      return pathPrefixes.map((prefix) => ({
        protocol: "https" as const,
        hostname: url.hostname,
        pathname: `${prefix.replace(/\/$/, "")}/**`,
      }));
    } catch {
      return [];
    }
  });
}
