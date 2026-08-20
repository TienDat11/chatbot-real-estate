"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Typography } from "antd";
import { CarOutlined, EnvironmentOutlined, UnorderedListOutlined } from "@ant-design/icons";
import type { NearbyPlace } from "@rag-ragre/contracts";
// Required for maplibre markers (position: absolute, anchoring) and controls.
// Without it markers render as static elements that stretch the canvas container.
import "maplibre-gl/dist/maplibre-gl.css";

/* ===================== senior-first design tokens ===================== */
const NAVY = "#1F46A8";
const INK = "#1A2233";
const MUTED = "#5B6478";

interface FilterDef { key: string; label: string; kinds: string[] | null; }
// Filter buckets map the seed catalog's kinds to human categories.
// "Ăn uống" = food markets (Chợ Mai, Chợ Chiều, Chợ Hàn, Chợ Mân Thái) plus any
// future restaurant/cafe kinds; "Mua sắm" = supermarkets/malls (Co.opmart, GO!).
// Kept disjoint so a place never matches two chips.
const FILTERS: FilterDef[] = [
  { key: "all", label: "Tất cả", kinds: null },
  { key: "food", label: "Ăn uống", kinds: ["market","restaurant","cafe","coffee","food","bar","bakery"] },
  { key: "school", label: "Trường học", kinds: ["school","university","college","kindergarten","high_school","primary_school"] },
  { key: "health", label: "Y tế", kinds: ["hospital","clinic","pharmacy","doctor","dentist","medical"] },
  { key: "shopping", label: "Mua sắm", kinds: ["supermarket","shopping_mall","mall","store","shop"] },
  { key: "fun", label: "Giải trí", kinds: ["tourist_attraction","park","museum","beach","natural_feature","bridge","cinema","place_of_worship","zoo"] },
];

function matchesFilter(filter: FilterDef, place: NearbyPlace): boolean {
  if (!filter.kinds) return true;
  return place.kinds.some((k) => filter.kinds!.includes(k));
}

function fmtDistance(m?: number): string {
  if (m === undefined || m === null || Number.isNaN(m)) return "";
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

export interface MapPanelProps {
  places: NearbyPlace[];
  project?: { lat: number; lng: number; name?: string };
  tileUrl?: string;
  placeZoom?: number;
  mode?: "map" | "list";
  onModeChange?: (mode: "map" | "list") => void;
}

// Verified on-site project pin: Le Van Luong x Le Duc Tho junction, Tho Quang.
// OSM street geometry crosses at ~16.1048, 108.2553; Google Maps pin for the
// project lot (60-62 Le Van Luong) = 16.1056072, 108.2563337. Center chosen to
// keep the project star + nearby places visible at the default zoom.
export const DEFAULT_PROJECT: { lat: number; lng: number; name: string } =
  { lat: 16.1052, lng: 108.2558, name: "The Camellia" };
const OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

function dirUrl(p: NearbyPlace): string {
  const dest = encodeURIComponent(p.lat + "," + p.lng);
  const origin = encodeURIComponent(`${DEFAULT_PROJECT.lat},${DEFAULT_PROJECT.lng}`);
  return `https://www.google.com/maps/dir/?api=1&origin=${origin}&destination=${dest}`;
}

/* marker builders ------------------------------------------------------- */
function projectMarkerElement(name: string): HTMLButtonElement {
  const el = document.createElement("button");
  el.type = "button";
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", `Dự án ${name}`);
  el.title = name;
  el.style.cssText = `width:56px;height:56px;border-radius:50%;background:${NAVY};color:#fff;border:2px solid #fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 2px 6px rgba(31,70,168,.35);`;
  const star = document.createElement("span");
  star.style.cssText = "font-size:26px;line-height:1;display:inline-flex;pointer-events:none;";
  star.innerHTML = "&#9733;";
  el.appendChild(star);
  return el;
}

function placeMarkerElement(p: NearbyPlace): HTMLButtonElement {
  const el = document.createElement("button");
  el.type = "button";
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", `${p.name}${p.rating ? " - " + p.rating : ""}`);
  el.title = p.name;
  el.style.cssText = `width:44px;height:44px;border-radius:50%;background:#fff;border:2px solid ${NAVY};display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 1px 3px rgba(26,34,51,.2);`;
  el.innerHTML = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="${NAVY}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>`;
  return el;
}

function projectLabelElement(name: string): HTMLDivElement {
  const el = document.createElement("div");
  el.textContent = name;
  el.style.cssText = `font-size:14px;font-weight:700;color:#fff;background:${NAVY};border-radius:6px;padding:2px 8px;white-space:nowrap;pointer-events:none;box-shadow:0 1px 3px rgba(26,34,51,.3);`;
  return el;
}

/* ======================================================================== */
export function MapPanel({
  places,
  project = DEFAULT_PROJECT,
  tileUrl = OSM_TILE,
  placeZoom = 16,
  mode: controlledMode,
  onModeChange,
}: MapPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  const markersRef = useRef<{ remove: () => void }[]>([]);
  const popupRef = useRef<{ remove: () => void } | null>(null);
  const listItemRefs = useRef<Map<string, HTMLLIElement | null>>(new Map());
  // Uncontrolled fallback so the panel still works when rendered standalone.
  const [internalMode, setInternalMode] = useState<"map" | "list">("map");
  const mode = controlledMode ?? internalMode;
  const setMode = (next: "map" | "list") => {
    if (onModeChange) onModeChange(next);
    else setInternalMode(next);
  };
  const [activeFilter, setActiveFilter] = useState<FilterDef>(FILTERS[0]);
  const [highlightKey, setHighlightKey] = useState<string | null>(null);
  const highlightTimer = useRef<number | null>(null);

  const sorted = useMemo(() => {
    return [...places].sort((a, b) => (a.distance_m ?? Infinity) - (b.distance_m ?? Infinity));
  }, [places]);

  const filtered = useMemo(() => {
    return sorted.filter((p) => matchesFilter(activeFilter, p));
  }, [sorted, activeFilter]);

  const keyOf = (p: NearbyPlace, i: number) => `${p.name}#${i}`;

  // Bumped when the map style finishes loading so the marker effect re-runs.
  const [markerTick, setMarkerTick] = useState(0);

  /* Build the Map once (client-only dynamic import). */
  useEffect(() => {
    const el = containerRef.current;
    // Tear down when the container is hidden/detached (list mode) so the map
    // is rebuilt fresh when the user returns to map mode (not a blank canvas).
    if (mode !== "map" || !el) {
      if (mapRef.current) { mapRef.current.remove(); mapRef.current = null; }
      return;
    }
    if (mapRef.current) return;
    let disposed = false;
    (async () => {
      const maplibre = await import("maplibre-gl");
      if (disposed) return;
      const map = new maplibre.Map({
        container: el,
        style: {
          version: 8,
          sources: { osm: { type: "raster", tiles: [tileUrl], tileSize: 256, attribution: OSM_ATTR } },
          layers: [{ id: "osm", type: "raster", source: "osm" }],
        },
        center: [project.lng, project.lat],
        zoom: 13.5,
      });
      mapRef.current = map;
      // Wait for style so isStyleLoaded() is true for the marker effect.
      map.on("load", () => setMarkerTick((t) => t + 1));
    })();
    return () => { disposed = true; if (mapRef.current) { mapRef.current.remove(); mapRef.current = null; } };
  }, [tileUrl, project.lng, project.lat, mode]);

  /* Recreate markers when data/filter/map become ready. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || mode !== "map") return;
    if (!map.isStyleLoaded()) return;
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];
    if (popupRef.current) { popupRef.current.remove(); popupRef.current = null; }
    (async () => {
      const maplibre = await import("maplibre-gl");
      const mapRefHere = mapRef.current;
      if (!mapRefHere) return;
      markersRef.current.push(
        new maplibre.Marker({ element: projectMarkerElement(project.name ?? "The Camellia") })
          .setLngLat([project.lng, project.lat]).addTo(mapRefHere)
      );
      markersRef.current.push(
        new maplibre.Marker({ element: projectLabelElement(project.name ?? "The Camellia") })
          .setLngLat([project.lng, project.lat + 0.0012]).addTo(mapRefHere)
      );
      filtered.forEach((p, i) => {
        const el = placeMarkerElement(p);
        const key = keyOf(p, i);
        el.addEventListener("click", () => {
          setHighlightKey(key);
          if (highlightTimer.current) window.clearTimeout(highlightTimer.current);
          highlightTimer.current = window.setTimeout(() => setHighlightKey(null), 2000);
          const li = listItemRefs.current.get(key);
          if (li) li.scrollIntoView({ block: "nearest", behavior: "smooth" });
        });
        markersRef.current.push(new maplibre.Marker({ element: el }).setLngLat([p.lng, p.lat]).addTo(mapRefHere));
      });
    })();
  }, [mode, filtered, markerTick, project, places]);

  /* Clean up highlight timer on unmount. */
  useEffect(() => {
    return () => { if (highlightTimer.current) window.clearTimeout(highlightTimer.current); };
  }, []);

  /* List item picked: fly to + popup. */
  function flyToMaybe(p: NearbyPlace) {
    const map = mapRef.current;
    if (!map) { setMode("map"); return; }
    setMode("map");
    map.flyTo({ center: [p.lng, p.lat], zoom: placeZoom, duration: 800 });
    (async () => {
      const maplibre = await import("maplibre-gl");
      const mapHere = mapRef.current;
      if (!mapHere) return;
      if (popupRef.current) { popupRef.current.remove(); popupRef.current = null; }
      const body = document.createElement("div");
      body.className = "map-panel-popup";
      const name = document.createElement("div");
      name.textContent = p.name;
      name.style.cssText = `font-size:16px;font-weight:700;color:${INK};`;
      body.appendChild(name);
      if (p.address) {
        const a = document.createElement("div");
        a.textContent = p.address;
        a.style.cssText = `font-size:16px;color:${MUTED};`;
        body.appendChild(a);
      }
      const d = document.createElement("div");
      d.textContent = `${p.kinds[0] ?? "Tiện ích"} · ${fmtDistance(p.distance_m)}`;
      d.style.cssText = `font-size:16px;color:${MUTED};margin:2px 0 10px;`;
      body.appendChild(d);
      const cta = document.createElement("a");
      cta.href = dirUrl(p);
      cta.target = "_blank";
      cta.rel = "noopener noreferrer";
      cta.textContent = "Chỉ đường";
      cta.style.cssText = `display:flex;align-items:center;justify-content:center;width:100%;height:48px;background:${NAVY};color:#fff;border-radius:8px;text-decoration:none;font-size:16px;font-weight:600;`;
      body.appendChild(cta);
      popupRef.current = new maplibre.Popup({ closeButton: true, closeOnClick: false, offset: 18 })
        .setLngLat([p.lng, p.lat]).setDOMContent(body).addTo(mapHere);
    })();
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {/* Panel header: title + mode tabs. Distinct visual from the parent
          Segmented (smaller, bordered, inset) so the two levels never confuse. */}
      <div style={{ padding: "10px 12px 6px" }}>
        <Typography.Text
          strong
          style={{ fontSize: 13, color: MUTED, textTransform: "uppercase", letterSpacing: 0.5 }}
        >
          Bản đồ &amp; Tiện ích
        </Typography.Text>
        <div
          role="tablist"
          aria-label="Chế độ hiển thị bản đồ"
          style={{
            display: "flex",
            gap: 6,
            marginTop: 8,
            background: "#EEF1F6",
            borderRadius: 12,
            padding: 4,
          }}
        >
          <button
            type="button"
            role="tab"
            aria-selected={mode === "map"}
            onClick={() => setMode("map")}
            style={{
              flex: 1,
              height: 40,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
              border: "none",
              borderRadius: 9,
              background: mode === "map" ? "#FFFFFF" : "transparent",
              color: mode === "map" ? NAVY : MUTED,
              fontWeight: 600,
              fontSize: 14,
              cursor: "pointer",
              boxShadow: mode === "map" ? "0 1px 3px rgba(26,34,51,.14)" : "none",
            }}
          >
            <EnvironmentOutlined /> Xem bản đồ
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "list"}
            onClick={() => setMode("list")}
            style={{
              flex: 1,
              height: 40,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
              border: "none",
              borderRadius: 9,
              background: mode === "list" ? "#FFFFFF" : "transparent",
              color: mode === "list" ? NAVY : MUTED,
              fontWeight: 600,
              fontSize: 14,
              cursor: "pointer",
              boxShadow: mode === "list" ? "0 1px 3px rgba(26,34,51,.14)" : "none",
            }}
          >
            <UnorderedListOutlined /> Danh sách
          </button>
        </div>
      </div>
      <div style={{ padding: "0 12px 10px", overflowX: "auto", display: "flex", gap: 8, flexShrink: 0 }}>
        {FILTERS.map((f) => {
          const active = activeFilter.key === f.key;
          return (
            <button
              key={f.key}
              type="button"
              aria-pressed={active}
              onClick={() => setActiveFilter(f)}
              style={{
                height: 48,
                padding: "0 18px",
                // Constant border width (2px) on every chip so toggling the
                // active state never changes the element's box size; the
                // inactive state just shows a transparent border.
                border: "2px solid " + (active ? NAVY : "transparent"),
                borderRadius: 999,
                background: active ? "#EEF2FB" : "#FFFFFF",
                color: active ? NAVY : MUTED,
                fontWeight: 600,
                fontSize: 16,
                cursor: "pointer",
                whiteSpace: "nowrap",
              }}
            >
              {f.label}
            </button>
          );
        })}
      </div>
      <div style={{ flex: 1, minHeight: 0, position: "relative" }}>
        {mode === "list" ? (
          <PlaceList
            places={filtered}
            itemRefs={listItemRefs}
            highlightKey={highlightKey}
            onPick={flyToMaybe}
          />
        ) : (
          <div ref={containerRef} style={{ position: "absolute", inset: 0, filter: "saturate(0.85) contrast(1.02)" }} />
        )}
      </div>
    </div>
  );
}

/* ---- list body ---- */
function PlaceList({
  places,
  itemRefs,
  highlightKey,
  onPick,
}: {
  places: NearbyPlace[];
  itemRefs: React.MutableRefObject<Map<string, HTMLLIElement | null>>;
  highlightKey: string | null;
  onPick: (p: NearbyPlace) => void;
}) {
  return (
    <div style={{ height: "100%", overflowY: "auto" }}>
      <Typography.Text strong style={{ display: "block", padding: "6px 14px 4px", fontSize: 14, color: MUTED, textTransform: "uppercase", letterSpacing: 0.4 }}>
        Gần dự án nhất
      </Typography.Text>
      <ul style={{ listStyle: "none", margin: 0, padding: "0 12px 16px" }}>
        {places.map((p, i) => {
          const key = `${p.name}#${i}`;
          const active = highlightKey === key;
          return (
            <li
              key={key}
              ref={(node) => { itemRefs.current.set(key, node); }}
              style={{
                background: active ? "#FFF4C2" : "#FFFFFF",
                border: "1px solid #E9ECF2",
                borderRadius: 12,
                padding: "12px 14px",
                margin: "0 0 10px",
                transition: "background .3s",
              }}
            >
              <button
                type="button"
                onClick={() => onPick(p)}
                aria-label={`${p.name}, cách ${fmtDistance(p.distance_m)}`}
                style={{ display: "block", width: "100%", textAlign: "left", background: "none", border: "none", padding: 0, cursor: "pointer" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ fontSize: 17, fontWeight: 600, color: INK, flex: 1, minWidth: 0 }}>{p.name}</span>
                  <span style={{ fontSize: 14, color: MUTED, whiteSpace: "nowrap" }}>{fmtDistance(p.distance_m)}</span>
                </div>
                <div style={{ fontSize: 14, color: MUTED, marginTop: 2 }}>{p.kinds[0] ?? "Tiện ích"}</div>
                {p.address && <div style={{ fontSize: 14, color: MUTED, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginTop: 2 }}>{p.address}</div>}
              </button>
              <Button
                type="primary"
                href={dirUrl(p)}
                target="_blank"
                icon={<CarOutlined />}
                block
                style={{ marginTop: 10, height: 48, fontSize: 16, background: NAVY, fontWeight: 600 }}
              >
                Chỉ đường
              </Button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
