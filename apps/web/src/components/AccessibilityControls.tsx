"use client";

import { useCallback, useEffect, useSyncExternalStore } from "react";
import { C, RADIUS } from "@/lib/tokens";

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

// External store so SSR + hydration render identically (level 0) and the
// persisted scale is picked up only after the client mounts. Reading
// localStorage in a useState initializer would cause a hydration mismatch.

function readLevel(): number {
  if (typeof window === "undefined") return 0;
  const saved = LEVELS.findIndex((l) => {
    try { return window.localStorage.getItem(STORAGE_KEY) === l.key; } catch { return false; }
  });
  return saved >= 0 ? saved : 0;
}

// Cache the snapshot so useSyncExternalStore sees a stable reference (no
// infinite re-render loop from a fresh value on every read).
let cached: number | null = null;
function getSnapshot(): number {
  if (cached === null) cached = readLevel();
  return cached;
}

function subscribe(onStoreChange: () => void): () => void {
  window.addEventListener("storage", onStoreChange);
  window.addEventListener("ragre:font-scale", onStoreChange);
  return () => {
    window.removeEventListener("storage", onStoreChange);
    window.removeEventListener("ragre:font-scale", onStoreChange);
  };
}

function syncClass(level: number): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  LEVELS.forEach((l) => root.classList.remove(l.key));
  root.classList.add(LEVELS[level].key);
}

export function AccessibilityControls() {
  const level = useSyncExternalStore(subscribe, getSnapshot, () => 0);

  // Apply the scale class to <html> (DOM mutation only, no setState).
  useEffect(() => {
    syncClass(level);
  }, [level]);

  const apply = useCallback((idx: number) => {
    syncClass(idx);
    try { window.localStorage.setItem(STORAGE_KEY, LEVELS[idx].key); } catch {}
    cached = idx;
    window.dispatchEvent(new Event("ragre:font-scale"));
  }, []);

  return (
    <div role="group" aria-label="Cỡ chữ" style={{ display: "flex", gap: 4, alignItems: "center" }}>
      {LEVELS.map((l, i) => (
        <button
          key={l.key}
          type="button"
          aria-pressed={level === i}
          aria-label={"Cỡ chữ " + l.label}
          onClick={() => apply(i)}
          style={{
            minWidth: 40,
            height: 40,
            borderRadius: RADIUS.small,
            border: level === i ? "2px solid " + C.primary : "1px solid " + C.borderStrong,
            background: level === i ? C.primarySoft : C.surface,
            color: C.text,
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