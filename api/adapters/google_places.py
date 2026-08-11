"""Google Places adapter — GeoPort via the Places Nearby Search HTTP API.

Queries the provider endpoint with httpx and maps results onto GeoPlace. Never
raises: any network/provider failure returns an empty GeoResult with degraded=True
so the pipeline falls back gracefully instead of crashing.
"""

from __future__ import annotations

import logging

import httpx

from api.ports.geo import GeoPlace, GeoPort, GeoResult

logger = logging.getLogger("api.adapters.google_places")

NEARBY_ENDPOINT = "/nearbysearch/json"
TIMEOUT_S = 3.0


class GooglePlaces(GeoPort):
    """GeoPort adapter over the Google Places Nearby Search API."""

    def __init__(self, api_key: str, base_url: str, radius_m: int = 5000):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.radius_m = radius_m

    async def places_around(
        self,
        lat: float,
        lng: float,
        radius_m: int,
        kinds: list[str] | None = None,
    ) -> GeoResult:
        if not self.api_key or not self.base_url:
            return GeoResult([], degraded=True, error="google_places: missing config")

        params: dict = {
            "location": f"{lat},{lng}",
            "radius": radius_m or self.radius_m,
            "key": self.api_key,
        }
        if kinds:
            params["type"] = kinds[0]
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                resp = await client.get(self.base_url + NEARBY_ENDPOINT, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — provider failure degrades
            logger.warning("google places failed: %s", exc)
            return GeoResult([], degraded=True, error=str(exc))

        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            return GeoResult([], degraded=True, error=f"google places status: {data.get('status')}")

        places = [
            GeoPlace(
                name=r.get("name") or "?",
                kinds=(r.get("types") or [])[:3],
                lat=float((r.get("geometry") or {}).get("location", {}).get("lat", 0.0)),
                lng=float((r.get("geometry") or {}).get("location", {}).get("lng", 0.0)),
                address=r.get("vicinity"),
                rating=r.get("rating"),
            )
            for r in (data.get("results") or [])
        ]
        return GeoResult(places=places, degraded=False)
