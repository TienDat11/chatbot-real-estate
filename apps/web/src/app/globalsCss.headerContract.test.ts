/**
 * FR-23 / R8 — static globals.css header contract.
 *
 * jsdom cannot lay out the responsive header, so the viewport behavior is
 * pinned as a deterministic source contract on globals.css: the header is a
 * LIGHT surface (no navy gradient / no on-dark ink), chip boundaries clear
 * the 3:1 non-text floor, the hot-contact CTA styles exist, and the narrow
 * phone viewport collapse for the project chip is preserved.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const css = readFileSync(fileURLToPath(new URL("./globals.css", import.meta.url)), "utf8");

function ruleBody(selector: string): string {
  const idx = css.indexOf(selector);
  if (idx === -1) return "";
  const open = css.indexOf("{", idx);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

describe("globals.css header contract (FR-23)", () => {
  it("defines exactly one .app-header block and it is a light surface", () => {
    expect(css.split(".app-header {").length - 1).toBe(1);
    const body = ruleBody(".app-header {");
    expect(body).toContain("var(--c-surface)");
    expect(body).toContain("var(--c-text)");
    // No bright-on-light ink and no gradient history is allowed back.
    expect(body).not.toContain("--c-on-dark");
    expect(body).not.toContain("linear-gradient");
  });

  it("never wires onDark ink into the header or its chips", () => {
    const chip = ruleBody(".header-chip {");
    const accent = ruleBody(".header-chip--accent {");
    expect(chip).toContain("var(--c-primary)");
    expect(chip).not.toContain("--c-on-dark");
    expect(accent).not.toContain("--c-on-dark");
  });

  it("uses >= 3:1 boundary tokens on both chip variants", () => {
    expect(ruleBody(".header-chip {")).toContain("var(--c-border-hover)");
    expect(ruleBody(".header-chip--accent {")).toContain("var(--c-gold-strong)");
  });

  it("pins the two boundary hexes that clear the non-text floor", () => {
    expect(css).toContain("--c-border-hover: #6f86a0"); // 3.75:1 on white
    expect(css).toContain("--c-gold-strong: #8f6626"); // 5.13:1 on white
  });

  it("keeps the gold hot badge readable with white ink", () => {
    const body = ruleBody(".chip-gold {");
    expect(body).toContain("#ffffff");
    expect(body).not.toContain("#0e2a47");
  });

  it("styles the actionable hotline CTA (FR-24)", () => {
    expect(ruleBody(".header-hotline-chip {")).toContain("cursor: pointer");
  });
});

describe("globals.css responsive header contract (viewport)", () => {
  it("collapses the project chip onto its own row on narrow phones", () => {
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.header-chip--accent \{[\s\S]*?flex: 1 1 160px/);
  });

  it("keeps the A/A+/A++ font-scale hooks wired at the root", () => {
    for (const scale of ["html.font-scale-1", "html.font-scale-2", "html.font-scale-3"]) {
      expect(css).toContain(scale);
    }
  });

  it("keeps a global visible keyboard focus ring", () => {
    expect(css).toMatch(/:focus-visible \{[\s\S]*?outline:/);
  });
});
