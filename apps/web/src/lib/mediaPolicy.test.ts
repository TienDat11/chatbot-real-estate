import { beforeEach, describe, expect, it, vi } from "vitest";
import { filterPublicImages, filterPublicVideos, getConfiguredMediaPatterns, isAllowedPublicMediaUrl } from "@/lib/mediaPolicy";

const publicMediaConfig = JSON.stringify([
  {
    origin: "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev",
    pathPrefixes: ["/media/video/", "/images/matbang/", "/images/banggia/", "/images/camellia/", "/images/soleil/"],
  },
]);

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_MEDIA_ORIGINS_JSON", publicMediaConfig);
});

describe("public media policy", () => {
  it("accepts configured banggia media and rejects unconfigured namespaces", () => {
    expect(isAllowedPublicMediaUrl("https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/images/banggia/price.pdf")).toBe(true);
    expect(isAllowedPublicMediaUrl("https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/private/a.png")).toBe(false);
  });

  it("rejects foreign and private origins even when their paths are allowed", () => {
    expect(isAllowedPublicMediaUrl("https://evil.example/images/banggia/price.pdf")).toBe(false);
    expect(isAllowedPublicMediaUrl("https://127.0.0.1/images/banggia/price.pdf")).toBe(false);
    expect(isAllowedPublicMediaUrl("http://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/images/banggia/price.pdf")).toBe(false);
  });

  it.each([
    "https://10.20.30.40/images/banggia/price.pdf",
    "https://172.16.0.1/images/banggia/price.pdf",
    "https://172.31.255.254/images/banggia/price.pdf",
    "https://169.254.1.1/images/banggia/price.pdf",
    "https://192.168.1.1/images/banggia/price.pdf",
    "https://localhost/images/banggia/price.pdf",
    "https://[::1]/images/banggia/price.pdf",
    "https://[fe80::1]/images/banggia/price.pdf",
    "https://[fd12:3456::1]/images/banggia/price.pdf",
  ])("rejects private, loopback, and link-local origin %s", (value) => {
    const origin = new URL(value).origin;
    vi.stubEnv("NEXT_PUBLIC_MEDIA_ORIGINS_JSON", JSON.stringify([{ origin, pathPrefixes: ["/images/banggia/"] }]));
    expect(isAllowedPublicMediaUrl(value)).toBe(false);
    vi.stubEnv("NEXT_PUBLIC_MEDIA_ORIGINS_JSON", JSON.stringify([
      {
        origin: "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev",
        pathPrefixes: ["/media/video/", "/images/matbang/", "/images/banggia/", "/images/camellia/", "/images/soleil/"],
      },
    ]));
  });

  it("filters invalid rows without changing backend facts", () => {
    expect(filterPublicImages([{ url_cdn: "https://evil.example/a" } as never])).toEqual([]);
    expect(filterPublicVideos([{ url_cdn: "https://pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev/media/video/a.mp4" } as never])).toHaveLength(1);
  });

  it("derives narrow image patterns from environment config", () => {
    expect(getConfiguredMediaPatterns()).toEqual([
      { protocol: "https", hostname: "pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev", pathname: "/media/video/**" },
      { protocol: "https", hostname: "pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev", pathname: "/images/matbang/**" },
      { protocol: "https", hostname: "pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev", pathname: "/images/banggia/**" },
      { protocol: "https", hostname: "pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev", pathname: "/images/camellia/**" },
      { protocol: "https", hostname: "pub-90e3022fb09146c1a740a85f96ed5be7.r2.dev", pathname: "/images/soleil/**" },
    ]);
  });
});
