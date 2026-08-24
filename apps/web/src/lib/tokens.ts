/**
 * RAG Real Estate design tokens (web app).
 *
 * Premium proptech visual system: deep navy + charcoal surfaces with warm
 * terracotta/gold accents on a warm cream ground. Single source of truth for
 * colors, radii and shadows — components import these constants so every
 * surface, border, chip and button stays on one coherent ramp.
 */
export const C = {
  // Brand deep navy (primary action + links)
  primary: "#0E2A47",
  primaryHover: "#0A2139",
  primarySoft: "#E8EEF7", // selected / tinted fills
  primaryBorder: "#C9D8EC", // tinted borders

  // Premium accents
  gold: "#C9A24B",
  goldHover: "#B78F3D",
  goldSoft: "#F7F0DF",
  goldBorder: "#E4D3A8",
  // Terracotta darkened for WCAG AA: white foreground reaches ~5.2:1 on the
  // lighter end of the CTA gradient (and terracotta-as-text on cream >= 4.5:1).
  terracotta: "#A8502E",
  terracottaHover: "#8F4527",
  terracottaSoft: "#F9ECE4",
  terracottaBorder: "#EACDBE",

  // Charcoal / dark surfaces (header, hero, dark cards)
  charcoal: "#1B2737",
  charcoalDeep: "#141D2B",
  charcoalSoft: "#263447",

  // Neutrals (one warm ramp)
  bg: "#FAF7F2", // page background (warm cream)
  surface: "#FFFFFF", // cards / bubbles
  surfaceAlt: "#F3EFE7", // subtle inset / code / table header
  border: "#E9E2D6", // hairline borders
  borderStrong: "#D8CFBF", // inputs, stronger separators

  // Text (hierarchy)
  text: "#1A2233",
  textMuted: "#5B6478",
  textFaint: "#8A93A6",
  textGhost: "#ABB3C3",

  // Text on dark surfaces
  onDark: "#F5F1E8",
  onDarkMuted: "#C2CCDB",

  // Semantic status
  success: "#1E7F4F",
  successSoft: "#E7F4ED",
  warning: "#C96F4A",
  warningSoft: "#F9ECE4",
  danger: "#C0392B",
  dangerSoft: "#FBEAE7",
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
  card: "0 1px 4px rgba(20,29,43,0.06)",
  pop: "0 12px 32px rgba(20,29,43,0.16)",
  primary: "0 4px 14px rgba(14,42,71,0.28)",
  gold: "0 2px 12px rgba(201,162,75,0.35)",
} as const;

/** Type scale — body font follows the senior-first --fs-body CSS variable. */
export const FS = {
  body: "var(--fs-body, 17px)",
  bodyLine: "var(--fs-body-line, 28px)",
  sm: 14,
  xs: 12,
  caption: 11,
} as const;
