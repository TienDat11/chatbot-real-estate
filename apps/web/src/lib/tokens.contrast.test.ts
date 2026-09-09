import { describe, expect, it } from "vitest";
import { C, CONTROL_BORDER, CONTROL_TEXT, HEADER } from "@/lib/tokens";

/**
 * Deterministic WCAG 2.x contrast verification for the R6 (FR-21) control
 * tokens. Ratios are computed from the token hex values, so any change that
 * regresses accessibility fails here instead of drifting silently.
 */

function parseHex(hex: string): [number, number, number] {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) {
    throw new Error(`Unsupported color format for contrast math: ${hex}`);
  }
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

/** WCAG 2.1 relative luminance of an sRGB channel triple. */
function relativeLuminance([r, g, b]: [number, number, number]): number {
  const linear = [r, g, b].map((channel) => {
    const srgb = channel / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  });
  return (
    0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
  );
}

export function contrastRatio(fg: string, bg: string): number {
  const l1 = relativeLuminance(parseHex(fg));
  const l2 = relativeLuminance(parseHex(bg));
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

// Normal-text AA target on the two light surfaces antd controls render on.
const NORMAL_TEXT_MIN = 4.5;
const LARGE_OR_NON_TEXT_MIN = 3;
const LIGHT_SURFACES = [
  ["surface", C.surface],
  ["bg", C.bg],
] as const;

describe("R6 control-state contrast tokens", () => {
  it.each(LIGHT_SURFACES)(
    "placeholder text clears %s AA normal-text contrast (>= 4.5:1)",
    (_name, surface) => {
      expect(contrastRatio(CONTROL_TEXT.placeholder, surface)).toBeGreaterThanOrEqual(
        NORMAL_TEXT_MIN,
      );
    },
  );

  it.each(LIGHT_SURFACES)(
    "quaternary text clears %s AA normal-text contrast (>= 4.5:1)",
    (_name, surface) => {
      expect(contrastRatio(CONTROL_TEXT.quaternary, surface)).toBeGreaterThanOrEqual(
        NORMAL_TEXT_MIN,
      );
    },
  );

  it.each(LIGHT_SURFACES)(
    "disabled text keeps a perceptible %s affordance (>= 3:1)",
    (_name, surface) => {
      expect(contrastRatio(CONTROL_TEXT.disabled, surface)).toBeGreaterThanOrEqual(
        LARGE_OR_NON_TEXT_MIN,
      );
    },
  );

  it("disabled stays visually recessive below normal-text prominence", () => {
    // Disabled content must not compete with enabled controls.
    expect(contrastRatio(CONTROL_TEXT.disabled, C.surface)).toBeLessThan(4.5);
  });

  it.each(LIGHT_SURFACES)(
    "control hover border meets the %s non-text boundary target (>= 3:1)",
    (_name, surface) => {
      expect(contrastRatio(CONTROL_BORDER.hover, surface)).toBeGreaterThanOrEqual(
        LARGE_OR_NON_TEXT_MIN,
      );
    },
  );

  it.each(LIGHT_SURFACES)(
    "focus border (primary navy) exceeds the %s non-text target on focus",
    (_name, surface) => {
      expect(contrastRatio(C.primary, surface)).toBeGreaterThanOrEqual(
        LARGE_OR_NON_TEXT_MIN,
      );
    },
  );

  it("preserves base text hierarchy sanity", () => {
    // Guard so a bad ramp edit cannot slide past undetected.
    expect(contrastRatio(C.text, C.surface)).toBeGreaterThanOrEqual(NORMAL_TEXT_MIN);
    expect(contrastRatio(C.textMuted, C.surface)).toBeGreaterThanOrEqual(NORMAL_TEXT_MIN);
  });
});

/**
 * FR-23 / R8 header chrome. The `.app-header` renders as the LIGHT surface
 * (white fill), so every header pairing is computed against C.surface and the
 * chip fills — never `C.onDark`, whose white ink is invisible there.
 */
const HEADER_SURFACE = C.surface;
const HEADER_NON_TEXT_MIN = 3;

/** [pair name, ink, fill, required ratio] for every header text pairing. */
const HEADER_TEXT_PAIRINGS: Array<[string, string, string, number]> = [
  ["title / header surface", HEADER.title, HEADER_SURFACE, NORMAL_TEXT_MIN],
  ["subtitle / header surface", HEADER.subtitle, HEADER_SURFACE, NORMAL_TEXT_MIN],
  ["quota chip text / chip fill", HEADER.chipText, HEADER.chipFill, NORMAL_TEXT_MIN],
  ["project chip text / accent fill", HEADER.accentText, HEADER.accentFill, NORMAL_TEXT_MIN],
  ["AI-hỗ-trợ badge text / accent fill", HEADER.badgeText, HEADER.accentFill, NORMAL_TEXT_MIN],
  ["hot-CTA text / terracotta fill", HEADER.hotCtaText, HEADER.hotCtaBg, NORMAL_TEXT_MIN],
  ["hot-CTA text / terracotta hover", HEADER.hotCtaText, HEADER.hotCtaBgHover, NORMAL_TEXT_MIN],
];

/** [pair name, ink, surface] non-text (icons + boundaries) >= 3:1 pairings. */
const HEADER_NON_TEXT_PAIRINGS: Array<[string, string, string]> = [
  ["quota icon / chip fill", HEADER.iconAccent, HEADER.chipFill],
  ["project icon / accent fill", HEADER.iconAccent, HEADER.accentFill],
  ["chip boundary / header surface", HEADER.chipBorder, HEADER_SURFACE],
  ["accent chip boundary / header surface", HEADER.accentChipBorder, HEADER_SURFACE],
  ["A/A+/A++ selected border / surface", C.primary, HEADER_SURFACE],
  ["A/A+/A++ idle border / surface", CONTROL_BORDER.hover, HEADER_SURFACE],
];

describe("FR-23/R8 header chrome contrast", () => {
  it.each(HEADER_TEXT_PAIRINGS)(
    "%s clears WCAG AA normal text (>= %s:1)",
    (_name, fg, bg, min) => {
      expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(min);
    },
  );

  it.each(HEADER_NON_TEXT_PAIRINGS)(
    "%s clears the non-text floor (>= 3:1)",
    (_name, fg, bg) => {
      expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(HEADER_NON_TEXT_MIN);
    },
  );

  it.each([
    ["idle A/A+/A++ ink on surface", HEADER.title, HEADER_SURFACE],
    ["selected A/A+/A++ ink on selected fill", HEADER.title, HEADER.chipFill],
  ])("%s clears AA normal text (>= 4.5:1)", (_name, fg, bg) => {
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(NORMAL_TEXT_MIN);
  });

  it("documents why onDark ink is banned on the light header", () => {
    // Regression anchor: white onDark ink fails against the actual white
    // header surface; a re-introduction of inline onDark colors regresses
    // this suite instead of drifting silently.
    expect(contrastRatio(C.onDark, HEADER_SURFACE)).toBeLessThan(NORMAL_TEXT_MIN);
  });

  it("gold hot-badge keeps white ink above AA", () => {
    // .chip-gold flipped from navy (2.75:1) to white ink.
    expect(contrastRatio("#FFFFFF", C.gold)).toBeGreaterThanOrEqual(NORMAL_TEXT_MIN);
    expect(contrastRatio(C.primary, C.gold)).toBeLessThan(NORMAL_TEXT_MIN);
  });
});
