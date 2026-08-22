import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchGreetingMedia } from "@/lib/api";

describe("fetchGreetingMedia", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("maps backend images/videos into the payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          greeting: "text",
          images: [
            {
              image_id: "soleil-1",
              kind: "matbang",
              title: "Tòa D — CH01",
              caption: "Mặt bằng Tòa D — CH01",
              alt_text: "Mặt bằng Tòa D — CH01",
              url_cdn: "https://cdn.example/img.png",
              width: 1000,
              height: 800,
            },
          ],
          videos: [],
        }),
        { status: 200 }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const media = await fetchGreetingMedia("soleil");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/llms-hello",
      expect.objectContaining({ method: "POST" })
    );
    expect(media.images).toHaveLength(1);
    expect(media.images[0]?.url_cdn).toBe("https://cdn.example/img.png");
    expect(media.videos).toEqual([]);
  });

  it("defaults missing arrays to empty instead of undefined", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ greeting: "text" }), { status: 200 })
      )
    );

    const media = await fetchGreetingMedia("soleil");

    expect(media.images).toEqual([]);
    expect(media.videos).toEqual([]);
  });

  it("rejects on non-2xx so callers can treat enrichment as optional", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("boom", { status: 503 }))
    );

    await expect(fetchGreetingMedia("soleil")).rejects.toThrow("503");
  });
});
