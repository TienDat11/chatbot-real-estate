// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CONFIDENCE_STYLE, ConfidenceBadge } from "./ConfidenceBadge";

afterEach(() => cleanup());

describe("ConfidenceBadge", () => {
  it("labels each tier without verified/accuracy promises", () => {
    for (const tier of ["HIGH", "MEDIUM", "LOW"] as const) {
      const { unmount } = render(<ConfidenceBadge confidence={tier} />);
      expect(screen.getByTestId("confidence-badge").textContent).toBe(
        CONFIDENCE_STYLE[tier].label,
      );
      unmount();
    }
    // No tier's label may promise legal accuracy.
    for (const style of Object.values(CONFIDENCE_STYLE)) {
      expect(style.label).not.toMatch(/đảm bảo pháp lý|chính xác 100%/i);
    }
  });
});
