/**
 * RAG Real Estate design tokens (web app).
 *
 * Single source of truth for the navy/trust-first legal-chat product. Mirrors the
 * CSS custom properties in app/globals.css and the @rag-ragre/ui DESIGN_TOKENS.
 * Components import these constants so colors, radii and shadows stay consistent
 * (one accent, one neutral ramp, one corner-radius scale).
 */
export const C = {
  // Brand / accent (single accent — locked for the whole page)
  primary: "#1F46A8",
  primaryHover: "#17407F",
  primarySoft: "#EAF2FF", // selected / tinted fills
  primaryBorder: "#D7E4FF", // tinted borders

  // Neutrals (one ramp: warm-off-white surface on cool layout)
  bg: "#F7F8FA", // page background
  surface: "#FFFFFF", // cards / bubbles
  surfaceAlt: "#F4F6FA", // subtle inset / code / table header
  border: "#E9ECF2", // hairline borders
  borderStrong: "#D5DBE6", // inputs, stronger separators

  // Text (hierarchy)
  text: "#1A2233",
  textMuted: "#5B6478",
  textFaint: "#8A93A6",
  textGhost: "#ABB3C3",

  // Semantic status
  success: "#16A34A",
  successSoft: "#EAF7EF",
  warning: "#D97706",
  warningSoft: "#FDF3E3",
  danger: "#DC2626",
  dangerSoft: "#FDECEC",
} as const;

export const RADIUS = {
  pill: 999,
  card: 16,
  bubble: 16,
  input: 12,
  btn: 12,
  small: 8,
} as const;

export const SHADOW = {
  card: "0 1px 4px rgba(26,34,51,0.06)",
  pop: "0 4px 16px rgba(26,34,51,0.10)",
  primary: "0 1px 3px rgba(31,70,168,0.20)",
} as const;

/** Type scale — body font follows the senior-first --fs-body CSS variable. */
export const FS = {
  body: "var(--fs-body, 17px)",
  bodyLine: "var(--fs-body-line, 28px)",
  sm: 14,
  xs: 12,
  caption: 11,
} as const;
