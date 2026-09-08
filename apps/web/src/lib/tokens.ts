/**
 * RAG Real Estate design tokens (web app).
 *
 * Bright proptech visual system: white surfaces with restrained navy brand
 * accents. Single source of truth for colors, radii and shadows so shared
 * surfaces stay consistent across auth, chat, CRM, and admin.
 */
export const C = {
  // Brand deep navy (primary action + links)
  primary: "#0E2A47",
  primaryHover: "#0A2139",
  primarySoft: "#E8EEF7", // selected / tinted fills
  primaryBorder: "#C9D8EC", // tinted borders

  // Restrained navy brand accents
  gold: "#8A651A",
  goldHover: "#6E5012",
  goldSoft: "#FFF8E6",
  goldBorder: "#E8D39A",
  // Darkened warm action for WCAG AA on white surfaces.
  terracotta: "#934322",
  terracottaHover: "#75351B",
  terracottaSoft: "#FFF0E8",
  terracottaBorder: "#E8BFA9",

  // Navy support tones used sparingly for brand navigation and accents.
  charcoal: "#1E3550",
  charcoalDeep: "#142A43",
  charcoalSoft: "#EEF3F8",

  // Cool, bright neutrals keep the product light and readable.
  bg: "#F7F9FC",
  surface: "#FFFFFF",
  surfaceAlt: "#F1F5F9",
  border: "#DCE4ED",
  borderStrong: "#B8C7D8",

  // Text hierarchy with WCAG AA contrast on light surfaces. The faint tier
  // still clears 4.73:1 and the ghost tier was darkened from #8091A4 so the
  // smallest captions/placeholders clear 4.5:1 (ISSUE-5 FR-1).
  text: "#142A43",
  textMuted: "#46586D",
  textFaint: "#62758B",
  textGhost: "#5E7085",

  // Text on navy brand surfaces.
  onDark: "#FFFFFF",
  onDarkMuted: "#D9E6F2",

  // Semantic status — warning darkened from #C96F4A so warning text/icons
  // clear 4.5:1 on white (ISSUE-5 FR-1 contrast audit).
  success: "#1E7F4F",
  successSoft: "#E7F4ED",
  warning: "#B25B36",
  warningSoft: "#F9ECE4",
  danger: "#C0392B",
  dangerSoft: "#FBEAE7",
} as const;

/**
 * Ant Design control-state tiers (R6 / FR-21). Derived from the neutral text
 * ramp so placeholder/quaternary text stays >= 4.5:1 on both C.surface and
 * C.bg, while disabled content keeps a perceptible >= 3:1 affordance (WCAG
 * exempts inactive UI; this improves on antd's default rgba black).
 * Enforced deterministically by tokens.contrast.test.ts.
 */
export const CONTROL_TEXT = {
  placeholder: C.textGhost,
  quaternary: C.textGhost,
  disabled: "#7A8CA0",
} as const;

/**
 * Form-control boundary colors. Hover border clears the 3:1 non-text
 * target on light backgrounds; focus border uses the primary navy.
 * Declared BEFORE HEADER, which references CONTROL_BORDER.hover.
 */
export const CONTROL_BORDER = {
  hover: "#6F86A0",
} as const;

/**
 * Header chrome tokens (FR-23 / R8). The shared `.app-header` renders as a
 * LIGHT surface (globals.css light-surface wave overrides the old navy
 * gradient), so every header text/icon/border pairing below resolves against
 * C.surface — never against `C.onDark`, whose white ink is invisible there.
 * Each pairing is asserted deterministically in tokens.contrast.test.ts:
 * >= 4.5:1 for text, >= 3:1 for icons and control boundaries.
 */
export const HEADER = {
  title: C.text,
  subtitle: C.textGhost,
  chipText: C.primary,
  accentText: C.text,
  badgeText: C.gold,
  iconAccent: C.gold,
  chipFill: C.primarySoft,
  accentFill: C.goldSoft,
  // Solid boundary colors that clear the 3:1 non-text floor on the white
  // header surface (the previous translucent glass borders did not).
  chipBorder: CONTROL_BORDER.hover, // #6F86A0, 3.75:1 on surface
  accentChipBorder: "#8F6626", // gold-family, 5.13:1 on surface
  // Hot-contact CTA: terracotta fill with white ink (6.85:1 / 9.20:1 hover).
  hotCtaText: "#FFFFFF",
  hotCtaBg: C.terracotta,
  hotCtaBgHover: C.terracottaHover,
} as const;

export const RADIUS = {
  pill: 999,
  card: 18,
  bubble: 18,
  input: 12,
  btn: 12,
  small: 8,
} as const;

export const SHADOW = {
  card: "0 1px 3px rgba(20, 42, 67, 0.06), 0 6px 18px rgba(20, 42, 67, 0.04)",
  pop: "0 12px 28px rgba(20, 42, 67, 0.12)",
  primary: "0 3px 10px rgba(14, 42, 71, 0.18)",
  gold: "0 2px 10px rgba(138, 101, 26, 0.18)",
} as const;

/** Type scale — body font follows the senior-first --fs-body CSS variable. */
export const FS = {
  body: "var(--fs-body, 17px)",
  bodyLine: "var(--fs-body-line, 28px)",
  sm: 14,
  xs: 12,
  caption: 11,
} as const;
