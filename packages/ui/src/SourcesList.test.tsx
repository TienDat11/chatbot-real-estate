// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { Source } from "@rag-ragre/contracts";
import { SourcesList } from "./SourcesList";


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
afterEach(() => cleanup());

const LONG_SOURCE: Source = {
  title: "Quyết định pháp lý " + "x".repeat(180),
  section: "Điều khoản " + "y".repeat(120),
  kind: "van_ban_phan_phoi",
  effective_from: "2026-01-01",
} as Source;

describe("SourcesList containment", () => {
  it("bounds long source metadata and effective-date tags", () => {
    const { container } = render(<SourcesList sources={[LONG_SOURCE]} />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.style.minWidth).toBe("0px");
    expect(root.style.maxWidth).toBe("100%");
    expect(root.style.overflowWrap).toBe("anywhere");
    const title = container.querySelector(".ant-typography") as HTMLElement;
    expect(title).toBeTruthy();
    expect(title.style.maxWidth).toBe("100%");
    expect(title.style.overflowWrap).toBe("anywhere");
    const tag = container.querySelector(".ant-tag") as HTMLElement;
    expect(tag.style.maxWidth).toBe("100%");
    expect(tag.style.whiteSpace).toBe("normal");
    expect(tag.style.overflowWrap).toBe("anywhere");
  });
});
