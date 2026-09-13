import math

from app.detection.geo import EARTH_RADIUS_KM, haversine_distance_km


def test_haversine_distance_zero_for_identical_points():
    assert haversine_distance_km(40.0, -73.0, 40.0, -73.0) == 0.0


def test_haversine_distance_quarter_great_circle():
    # (0,0) to (0,90) is a quarter of the great circle: (pi/2) * R
    expected = (math.pi / 2) * EARTH_RADIUS_KM
    actual = haversine_distance_km(0.0, 0.0, 0.0, 90.0)
    assert abs(actual - expected) < 0.01


def test_haversine_distance_symmetric_and_matches_known_value():
    d1 = haversine_distance_km(51.5074, -0.1278, 40.7128, -74.0060)  # London-NYC
    d2 = haversine_distance_km(40.7128, -74.0060, 51.5074, -0.1278)
    assert abs(d1 - d2) < 1e-9
    assert 5500 < d1 < 5600  # known real-world great-circle distance ~5570km
