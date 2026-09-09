/** @vitest-environment jsdom */
import { describe, expect, it } from "vitest";
import { projectLabelElement } from "@/components/MapPanel";

describe("MapPanel compact project label", () => {
  it("renders short name while retaining full name as tooltip", () => {
    const label = projectLabelElement("The Soleil", "The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia)");
    expect(label.textContent).toBe("The Soleil");
    expect(label.title).toContain("Bộ sưu tập");
  });
});
