"""Geo port — nearby-places retrieval contract (Google Places + static fallback)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class GeoPlace:
    """One point-of-interest returned by a geo provider."""

    name: str
    kinds: list[str]
    lat: float
    lng: float
    distance_m: float | None = None
    address: str | None = None
    rating: float | None = None


@dataclass(frozen=True)
class GeoResult:
    """Places plus a degraded flag so callers can lower confidence on failure."""

    places: list[GeoPlace] = field(default_factory=list)
    degraded: bool = False
    error: str | None = None


@runtime_checkable
class GeoPort(Protocol):
    """Nearby places around a coordinate; never raises, returns GeoResult."""

    async def places_around(
        self,
        lat: float,
        lng: float,
        radius_m: int,
        kinds: list[str] | None = None,
    ) -> GeoResult: ...
