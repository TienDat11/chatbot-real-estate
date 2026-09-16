// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Video } from "@rag-ragre/contracts";
import { GreetingMedia } from "./GreetingMedia";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: () => ({
      matches: false,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

const VIDEO: Video = {
  video_id: "video-1",
  url_cdn: "https://example.test/media/video.mp4",
  title: "Dự án " + "x".repeat(160),
  kind: "brand",
} as Video;

describe("GreetingMedia containment", () => {
  it("bounds the rejected-media status path", () => {
    const { container, getByRole } = render(
      <GreetingMedia
        videos={[{ ...VIDEO, url_cdn: "https://invalid.example/video.mp4" }]}
        images={[]}
      />,
    );
    const root = container.firstElementChild as HTMLElement;
    expect(root.style.minWidth).toBe("0px");
    expect(root.style.maxWidth).toBe("100%");
    expect(root.style.overflowWrap).toBe("anywhere");
    expect(getByRole("status")).toBeTruthy();
  });

  it("bounds the video hero and dynamic overlay title", () => {
    vi.stubEnv(
      "NEXT_PUBLIC_MEDIA_ORIGINS_JSON",
      JSON.stringify([{ origin: "https://example.test", pathPrefixes: ["/media/"] }]),
    );
    const { container } = render(<GreetingMedia videos={[VIDEO]} images={[]} />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.style.minWidth).toBe("0px");
    expect(root.style.maxWidth).toBe("100%");
    const hero = container.querySelector(".greeting-media__header-row") as HTMLElement;
    expect(hero).toBeTruthy();
    expect(hero.style.minWidth).toBe("0px");
    expect(hero.style.maxWidth).toBe("100%");
    const title = Array.from(container.querySelectorAll(".ant-typography")).find((node) =>
      node.textContent?.includes(VIDEO.title),
    ) as HTMLElement;
    expect(title).toBeTruthy();
    expect(title.style.maxWidth).toBe("100%");
    expect(title.style.overflowWrap).toBe("anywhere");
  });

  it("renders the image gallery when public images are provided without video", () => {
    vi.stubEnv(
      "NEXT_PUBLIC_MEDIA_ORIGINS_JSON",
      JSON.stringify([{ origin: "https://example.test", pathPrefixes: ["/media/"] }]),
    );
    const { container } = render(
      <GreetingMedia
        videos={[]}
        images={[{
          image_id: "image-1",
          kind: "exterior",
          title: "Mặt tiền dự án",
          caption: null,
          alt_text: "Mặt tiền dự án",
          url_cdn: "https://example.test/media/image.jpg",
          width: 800,
          height: 600,
          score: 1,
        }]}
      />,
    );
    expect(container.querySelector("img")).toBeTruthy();
    expect(container.textContent).toContain("Mặt tiền dự án");
  });
});
