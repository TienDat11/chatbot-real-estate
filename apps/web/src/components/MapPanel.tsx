"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Modal, Typography } from "antd";
import { CarOutlined, EnvironmentOutlined, UnorderedListOutlined } from "@ant-design/icons";
import type { NearbyPlace } from "@rag-ragre/contracts";
import {
  createDirectionsFlow,
  type DirectionsFlow,
  type LineStringGeometry,
  type RouteDirections,
} from "@/lib/routeDirections";
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

/** Zoom used when the map opens and when the active project changes. */
export const MAP_PROJECT_ZOOM = 13.5;

const OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// Stable ids for the client-side route overlay so a new route (or a project
// change) can replace the previous polyline without leaking layers.
const ROUTE_SOURCE_ID = "directions-route";
const ROUTE_LAYER_ID = "directions-route-line";

/* project-change recenter ----------------------------------------------- */

/** Minimal maplibre surface `recenterForProject` needs (mockable in vitest). */
export interface RecenterMapLike {
  flyTo(opts: { center: [number, number]; zoom?: number; duration?: number }): void;
  getLayer?(id: string): unknown;
  getSource?(id: string): unknown;
  removeLayer?(id: string): void;
  removeSource?(id: string): void;
}

/**
 * Moves the map camera to a new project's geo center and drops the previous
 * project's route overlay. Two active projects sit kilometres apart, so a
 * project switch must never leave the old project's route polyline or camera
 * position on screen. Markers are removed separately by the caller (they live
 * outside the maplibre style graph), so this helper stays a pure map-side
 * function that vitest can drive with a mock map instance.
 */
export function recenterForProject(
  map: RecenterMapLike,
  project: { lat: number; lng: number },
  zoom = MAP_PROJECT_ZOOM
): void {
  if (map.getLayer?.(ROUTE_LAYER_ID)) map.removeLayer?.(ROUTE_LAYER_ID);
  if (map.getSource?.(ROUTE_SOURCE_ID)) map.removeSource?.(ROUTE_SOURCE_ID);
  map.flyTo({ center: [project.lng, project.lat], zoom, duration: 800 });
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
  // Consent-first directions (story 10.7): the modal opens before any
  // geolocation call, and the resolved location never leaves this component.
  const [directionsOpen, setDirectionsOpen] = useState(false);
  const [directionsBusy, setDirectionsBusy] = useState(false);
  const [routeBadge, setRouteBadge] = useState<RouteDirections | null>(null);
  const activeDirectionsRef = useRef<DirectionsFlow | null>(null);
  // A route approved while the map is absent (list mode) is drawn as soon as
  // the map finishes loading instead of being silently dropped.
  const pendingRouteRef = useRef<LineStringGeometry | null>(null);

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
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
        setRouteBadge(null);
        pendingRouteRef.current = null;
      }
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
        zoom: MAP_PROJECT_ZOOM,
      });
      mapRef.current = map;
      // Wait for style so isStyleLoaded() is true for the marker effect.
      map.on("load", () => {
        setMarkerTick((t) => t + 1);
        // Route approved before this map existed (list mode) gets drawn now.
        if (pendingRouteRef.current) {
          void drawRouteLine(pendingRouteRef.current);
          pendingRouteRef.current = null;
        }
      });
    })();
    return () => {
      disposed = true;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
        setRouteBadge(null);
        pendingRouteRef.current = null;
      }
    };
  }, [tileUrl, project.lng, project.lat, mode]);

  /* Recenter when the active project changes. The map object is reused (not
     rebuilt), so the camera + route overlay must move explicitly: two active
     projects sit kilometres apart and the old project's polyline must not
     survive the switch. Markers are refreshed by the markers effect below
     (it watches the same project lat/lng + name). */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || mode !== "map") return;
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];
    if (popupRef.current) { popupRef.current.remove(); popupRef.current = null; }
    recenterForProject(map as RecenterMapLike, project, MAP_PROJECT_ZOOM);
  }, [project.lat, project.lng, mode]);

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
      const cta = document.createElement("button");
      cta.type = "button";
      cta.textContent = "Chỉ đường";
      // Consent-first: only opens the location popup; geolocation and routing
      // happen after the visitor agrees (story 10.7).
      cta.addEventListener("click", () => requestDirections());
      cta.style.cssText = `display:flex;align-items:center;justify-content:center;width:100%;height:48px;background:${NAVY};color:#fff;border-radius:8px;border:none;text-decoration:none;font-size:16px;font-weight:600;cursor:pointer;`;
      body.appendChild(cta);
      popupRef.current = new maplibre.Popup({ closeButton: true, closeOnClick: false, offset: 18 })
        .setLngLat([p.lng, p.lat]).setDOMContent(body).addTo(mapHere);
    })();
  }

  /* ---- consent-first directions (story 10.7) ---- */

  // "Chỉ đường" always goes through the permission popup first. The flow is
  // created lazily on the click so SSR/static render never touches navigator.
  function requestDirections() {
    const flow = createDirectionsFlow(project, {
      geolocation: typeof navigator !== "undefined" ? navigator.geolocation : undefined,
    });
    activeDirectionsRef.current = flow;
    flow.begin();
    setDirectionsOpen(true);
  }

  // User accepted: resolve the location, then draw the OSRM route or fall back
  // to the 5.2 Google Maps deep-link when the location/route is unavailable.
  async function approveDirections() {
    const flow = activeDirectionsRef.current;
    if (!flow) return;
    setDirectionsBusy(true);
    try {
      const outcome = await flow.confirm();
      if (outcome.status === "route") {
        setRouteBadge(outcome);
        const map = mapRef.current;
        if (map && map.isStyleLoaded()) {
          await drawRouteLine(outcome.geometry);
        } else {
          // The map is absent (list mode) or still loading its style: keep the
          // geometry and let the map "load" handler draw it.
          pendingRouteRef.current = outcome.geometry;
          if (!map) setMode("map");
        }
      } else {
        clearRoute();
        window.open(outcome.url, "_blank", "noopener,noreferrer");
      }
    } finally {
      setDirectionsBusy(false);
      setDirectionsOpen(false);
    }
  }

  // User dismissed the popup (ESC / backdrop / close X): close ONLY. Never
  // trigger the deep-link fallback from a dismiss — closing a popup must not
  // open a new tab (popup trap, review M3).
  function dismissDirections() {
    setDirectionsOpen(false);
  }

  // User explicitly pressed "Không": an active decline keeps the 5.2 deep-link
  // behaviour (no origin coordinate, so no location is requested or shared).
  function declineDirections() {
    const flow = activeDirectionsRef.current;
    if (flow) {
      const outcome = flow.cancel();
      if (outcome.status === "fallback") {
        window.open(outcome.url, "_blank", "noopener,noreferrer");
      }
    }
    setDirectionsOpen(false);
  }

  function removeRouteOverlay() {
    const map = mapRef.current;
    if (!map) return;
    if (map.getLayer(ROUTE_LAYER_ID)) map.removeLayer(ROUTE_LAYER_ID);
    if (map.getSource(ROUTE_SOURCE_ID)) map.removeSource(ROUTE_SOURCE_ID);
  }

  function clearRoute() {
    removeRouteOverlay();
    setRouteBadge(null);
  }

  // Draws the OSRM polyline and fits the view to the whole route so the
  // visitor can read the full path, not just the tail.
  async function drawRouteLine(geometry: LineStringGeometry) {
    const map = mapRef.current;
    if (!map) return;
    const maplibre = await import("maplibre-gl");
    removeRouteOverlay();
    map.addSource(ROUTE_SOURCE_ID, {
      type: "geojson",
      data: geometry as unknown as GeoJSON.GeoJSON,
    });
    map.addLayer({
      id: ROUTE_LAYER_ID,
      type: "line",
      source: ROUTE_SOURCE_ID,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": NAVY, "line-width": 5, "line-opacity": 0.9 },
    });
    const [firstLng, firstLat] = geometry.coordinates[0];
    const bounds = new maplibre.LngLatBounds([firstLng, firstLat], [firstLng, firstLat]);
    geometry.coordinates.forEach(([lng, lat]) => bounds.extend([lng, lat]));
    bounds.extend([project.lng, project.lat]);
    map.fitBounds(bounds, { padding: 48, duration: 700 });
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
            onDirections={requestDirections}
          />
        ) : (
          <>
            <div ref={containerRef} style={{ position: "absolute", inset: 0, filter: "saturate(0.85) contrast(1.02)" }} />
            {routeBadge && (
              <div
                style={{
                  position: "absolute",
                  top: 12,
                  right: 12,
                  zIndex: 2,
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  background: "#FFFFFF",
                  border: `1px solid ${NAVY}`,
                  borderRadius: 10,
                  padding: "8px 14px",
                  boxShadow: "0 2px 8px rgba(26,34,51,.18)",
                }}
              >
                <CarOutlined style={{ color: NAVY, fontSize: 20 }} />
                <div>
                  <div style={{ fontSize: 16, fontWeight: 700, color: INK }}>{routeBadge.statsLabel}</div>
                  <div style={{ fontSize: 13, color: MUTED }}>Đường đi dự kiến</div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
      {/* Permission popup: explains why the location is needed and that it
          stays on the device before any geolocation call is made. */}
      <Modal
        open={directionsOpen}
        title="Cho phép lấy vị trí của bạn?"
        onOk={approveDirections}
        onCancel={dismissDirections}
        footer={
          <>
            {/* Explicit decline is the ONLY path to the deep-link fallback;
                dismiss paths (ESC/backdrop/X) go through dismissDirections so
                they cannot spawn a tab (review M3). */}
            <Button onClick={declineDirections} disabled={directionsBusy}>
              Không
            </Button>
            <Button
              type="primary"
              loading={directionsBusy}
              onClick={approveDirections}
            >
              {directionsBusy ? "Đang tính đường..." : "Đồng ý"}
            </Button>
          </>
        }
        centered
      >
        <p style={{ fontSize: 16, lineHeight: 1.65, color: INK, marginTop: 8 }}>
          Để vẽ đường đi từ vị trí hiện tại của bạn đến dự án trên bản đồ, ứng dụng cần biết vị trí của bạn.
        </p>
        <p style={{ fontSize: 16, lineHeight: 1.65, color: MUTED }}>
          Vị trí chỉ được sử dụng ngay trên máy này để tính đường đi, không được gửi đến máy chủ và không được lưu lại sau khi đóng trang.
        </p>
      </Modal>
    </div>
  );
}

/* ---- list body ---- */
function PlaceList({
  places,
  itemRefs,
  highlightKey,
  onPick,
  onDirections,
}: {
  places: NearbyPlace[];
  itemRefs: React.MutableRefObject<Map<string, HTMLLIElement | null>>;
  highlightKey: string | null;
  onPick: (p: NearbyPlace) => void;
  onDirections: () => void;
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
                icon={<CarOutlined />}
                block
                onClick={onDirections}
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
