// @vitest-environment jsdom
/**
 * FR-23 / R8 — AccessibilityControls (A/A+/A++) surface-aware styling.
 *
 * Pins the light-mode contract on the ChatPage header: dark ink via HEADER
 * tokens (never C.onDark), >= 3:1 idle boundaries, and working persistence.
 * The old `dark ? C.onDark : C.text` branch is what made the controls unread
 * against the light header; this suite regresses it deterministically.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AccessibilityControls } from "@/components/AccessibilityControls";
import { C, HEADER } from "@/lib/tokens";

const STORAGE_KEY = "ragre.font_scale";

/** jsdom may serialize hex as rgb(); accept either exact representation. */
function isSameColor(actual: string, hex: string): boolean {
  const n = parseInt(hex.slice(1), 16);
  const rgb = `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
  return actual.toLowerCase() === hex.toLowerCase() || actual === rgb;
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  document.documentElement.className = "";
  vi.restoreAllMocks();
});

describe("AccessibilityControls header chrome (FR-23)", () => {
  it("renders three focusable buttons with accessible labels", () => {
    render(<AccessibilityControls />);
    for (const label of ["Cỡ chữ A", "Cỡ chữ A+", "Cỡ chữ A++"]) {
      const btn = screen.getByRole("button", { name: label });
      expect(btn).not.toBeNull();
      btn.focus();
      expect(document.activeElement).toBe(btn);
    }
  });

  it("uses light-surface ink and boundaries, never onDark ink", () => {
    render(<AccessibilityControls />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(3);
    let selectedCount = 0;
    for (const btn of buttons) {
      // Light mode ink resolves against the white header surface.
      expect(isSameColor(btn.style.color, HEADER.title)).toBe(true);
      expect(isSameColor(btn.style.color, C.onDark)).toBe(false);
      // Boundary clears the 3:1 non-text floor in BOTH states: selected =
      // primary navy (12.5:1), idle = CONTROL_BORDER.hover (#6F86A0, 3.75:1).
      const pressed = btn.getAttribute("aria-pressed") === "true";
      expect(
        btn.style.border.includes(pressed ? C.primary : "#6F86A0") ||
          isSameColor(btn.style.borderColor, pressed ? C.primary : "#6F86A0"),
      ).toBe(true);
      if (pressed) selectedCount += 1;
    }
    expect(selectedCount).toBe(1);
  });

  it("selected state uses primary border + soft fill, aria-pressed tracked", () => {
    render(<AccessibilityControls />);
    const first = screen.getByRole("button", { name: "Cỡ chữ A" });
    expect(first.getAttribute("aria-pressed")).toBe("true");
    expect(
      first.style.border.includes(C.primary) ||
        isSameColor(first.style.borderColor, C.primary),
    ).toBe(true);

    const third = screen.getByRole("button", { name: "Cỡ chữ A++" });
    expect(third.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(third);
    expect(third.getAttribute("aria-pressed")).toBe("true");
    expect(first.getAttribute("aria-pressed")).toBe("false");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("font-scale-3");
  });

  it("keeps the opt-in dark branch available with white ink", () => {
    render(<div data-testid="dark-parent" style={{ background: C.charcoalDeep }}>
      <AccessibilityControls dark />
    </div>);
    const buttons = screen.getAllByRole("button");
    for (const btn of buttons) {
      // On a genuinely dark parent surface, white ink is correct.
      expect(isSameColor(btn.style.color, C.onDark)).toBe(true);
    }
  });
});
