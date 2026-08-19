"use client";

import { useEffect, useState } from "react";

/**
 * AccessibilityControls — global A/A+/A++ font-size toggle in the header.
 * Persists to localStorage("ragre.font_scale") and applies a root-level class
 * (font-scale-1/2/3) so components read the CSS variable --fs-body.
 */

const LEVELS = [
  { key: "font-scale-1", label: "A", size: 17 },
  { key: "font-scale-2", label: "A+", size: 19 },
  { key: "font-scale-3", label: "A++", size: 21 },
];

const STORAGE_KEY = "ragre.font_scale";

function storedLevel(): number {
  if (typeof window === "undefined") return 0;
  const saved = LEVELS.findIndex((l) => {
    try { return window.localStorage.getItem(STORAGE_KEY) === l.key; } catch { return false; }
  });
  return saved >= 0 ? saved : 0;
}

export function AccessibilityControls() {
  const [level, setLevel] = useState(storedLevel);

  // Re-apply the persisted scale on mount so the choice survives reloads.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    const idx = storedLevel();
    LEVELS.forEach((l) => root.classList.remove(l.key));
    root.classList.add(LEVELS[idx].key);
    setLevel(idx);
  }, []);

  const apply = (idx: number) => {
    const root = document.documentElement;
    LEVELS.forEach((l) => root.classList.remove(l.key));
    root.classList.add(LEVELS[idx].key);
    try { window.localStorage.setItem(STORAGE_KEY, LEVELS[idx].key); } catch {}
    setLevel(idx);
  };

  return (
    <div role="group" aria-label="Cỡ chữ" style={{ display: "flex", gap: 4, alignItems: "center" }}>
      {LEVELS.map((l, i) => (
        <button
          key={l.key}
          type="button"
          aria-pressed={level === i}
          aria-label={`Cỡ chữ ${l.label}`}
          onClick={() => apply(i)}
          style={{
            minWidth: 40,
            height: 40,
            borderRadius: 8,
            border: level === i ? "2px solid #1F46A8" : "1px solid #D5DBE6",
            background: level === i ? "#EAF2FF" : "#FFFFFF",
            color: "#1A2233",
            fontSize: l.size === 17 ? 14 : l.size === 19 ? 15 : 16,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          {l.label}
        </button>
      ))}
    </div>
  );
}
