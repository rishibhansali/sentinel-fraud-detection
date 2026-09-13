"""Great-circle distance between two points (haversine) — the inverse of
Phase 1's destination-point problem (scripts/augment.py computes a
destination given a start point, distance, and bearing; this computes
distance given two points). Not imported from scripts/augment.py:
scripts/ and backend/ are separate services with separate venvs in this
project's architecture, so there is no live import path between them.
Reuses the same EARTH_RADIUS_KM constant and the same non-flat-earth
rigor as Phase 1's math.
"""
import math

EARTH_RADIUS_KM = 6371.0


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c
