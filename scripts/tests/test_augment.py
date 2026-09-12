import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from augment import (
    NUM_USERS,
    assign_locations,
    assign_splits,
    assign_users_and_cards,
    generate_home_locations,
)


def test_generate_home_locations_deterministic_for_fixed_seed():
    rng1 = np.random.default_rng(42)
    rng2 = np.random.default_rng(42)
    lat1, lon1 = generate_home_locations(NUM_USERS, rng1)
    lat2, lon2 = generate_home_locations(NUM_USERS, rng2)
    assert np.array_equal(lat1, lat2)
    assert np.array_equal(lon1, lon2)
    assert lat1.shape == (NUM_USERS,)


def test_assign_users_and_cards_ranges_and_relationship():
    rng = np.random.default_rng(7)
    user_ids, card_ids = assign_users_and_cards(10_000, NUM_USERS, rng)
    assert user_ids.min() >= 0
    assert user_ids.max() < NUM_USERS
    assert np.array_equal(card_ids // 10, user_ids)
    assert set(np.unique(card_ids % 10)) <= {0, 1}


def _approx_km(lat0, lon0, lat1, lon1):
    r = 6371.0
    p1, p2 = math.radians(lat0), math.radians(lat1)
    dphi = math.radians(lat1 - lat0)
    dlmb = math.radians(lon1 - lon0)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def test_assign_locations_biases_far_geo_toward_fraud():
    rng = np.random.default_rng(123)
    home_lat, home_lon = generate_home_locations(NUM_USERS, rng)
    n = 20_000
    user_ids = rng.integers(0, NUM_USERS, size=n)
    class_labels = np.where(np.arange(n) < n // 2, 1, 0)  # first half fraud
    lat, lon = assign_locations(user_ids, class_labels, home_lat, home_lon, rng)

    distances = np.array([
        _approx_km(home_lat[user_ids[i]], home_lon[user_ids[i]], lat[i], lon[i])
        for i in range(n)
    ])
    far_fraud = (distances[class_labels == 1] > 200).mean()
    far_legit = (distances[class_labels == 0] > 200).mean()

    assert far_fraud > far_legit
    assert far_fraud > 0.15
    assert far_legit < 0.10


def test_assign_splits_deterministic_and_stratified_per_class():
    rng1 = np.random.default_rng(42)
    rng2 = np.random.default_rng(42)
    n = 100_000
    class_labels = np.where(np.arange(n) < n // 20, 1, 0)  # 5% fraud, like the real data

    splits1 = assign_splits(class_labels, rng1)
    splits2 = assign_splits(class_labels, rng2)

    assert np.array_equal(splits1, splits2)  # deterministic for a fixed seed
    assert set(np.unique(splits1)) == {"train", "val", "test"}

    for label in (0, 1):
        mask = class_labels == label
        counts = {v: (splits1[mask] == v).sum() for v in ("train", "val", "test")}
        total = mask.sum()
        assert abs(counts["train"] / total - 0.70) < 0.01
        assert abs(counts["val"] / total - 0.15) < 0.01
        assert abs(counts["test"] / total - 0.15) < 0.01


def test_assign_splits_no_row_appears_in_two_splits():
    rng = np.random.default_rng(7)
    n = 5_000
    class_labels = np.where(np.arange(n) < n // 10, 1, 0)
    splits = assign_splits(class_labels, rng)
    assert len(splits) == n
    assert all(v in ("train", "val", "test") for v in splits)
