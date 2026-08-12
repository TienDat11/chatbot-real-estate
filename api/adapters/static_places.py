"""Static geo adapter — GeoPort fallback from a local JSON catalog (no network).

Used when no Google Places key is configured. Loads the catalog once (lazy),
filters by requested kinds and radius, and returns the matching places. This is
the stable fallback, so it reports degraded=False; the pipeline marks the
fallback via config (geo_binding != google).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from api.ports.geo import GeoPlace, GeoPort, GeoResult

logger = logging.getLogger("api.adapters.static_places")


class StaticPlaces(GeoPort):
    """GeoPort adapter over a static JSON place catalog."""

    def __init__(self, path: str, radius_m: int = 10000):
        self.path = path
        self.radius_m = radius_m
        self._places: list[GeoPlace] | None = None

    def _load(self) -> list[GeoPlace]:
        """Read and parse the catalog once; bad/missing file degrades to empty."""
        if self._places is not None:
            return self._places
        places: list[GeoPlace] = []
        path = Path(self.path)
        if not path.exists():
            logger.warning("static places catalog missing: %s", path)
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for r in data.get("places") or []:
                    places.append(
                        GeoPlace(
                            name=r.get("name") or "?",
                            kinds=list(r.get("kinds") or []),
                            lat=float(r.get("lat", 0.0)),
                            lng=float(r.get("lng", 0.0)),
                            distance_m=r.get("distance_m"),
                            address=r.get("address"),
                            rating=r.get("rating"),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 — bad catalog degrades, not crashes
                logger.warning("static places catalog unreadable (%s): %s", path, exc)
        self._places = places
        return places

    async def places_around(
        self,
        lat: float,
        lng: float,
        radius_m: int,
        kinds: list[str] | None = None,
    ) -> GeoResult:
        radius = radius_m or self.radius_m
        try:
            places = [p for p in self._load() if p.distance_m is None or p.distance_m <= radius]
        except Exception as exc:  # noqa: BLE001 — catalog error degrades
            return GeoResult([], degraded=True, error=str(exc))
        if kinds:
            wanted = set(kinds)
            places = [p for p in places if wanted & set(p.kinds)]
        return GeoResult(places=places, degraded=False)
