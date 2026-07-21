"""Deterministic synthetic user/card/geo augmentation for the ULB dataset.

The source dataset's V1-V28/Amount/Class values are never touched here —
this module only invents identity and location fields that don't exist in
the original data. Reproducibility comes from a fixed seed plus a fixed
processing order, not from a literal hash-of-index formula. See
data/README.md for the documented rationale and exact ratios used.
"""
import numpy as np

NUM_USERS = 5000
NEAR_JITTER_KM_STD = 10.0
FAR_MIN_KM = 500.0
FAR_MAX_KM = 3000.0
FAR_FRACTION_FRAUD = 0.35
FAR_FRACTION_LEGIT = 0.03
KM_PER_DEGREE_LAT = 111.0


def generate_home_locations(num_users: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    lat = rng.uniform(-60.0, 70.0, size=num_users)
    lon = rng.uniform(-180.0, 180.0, size=num_users)
    return lat, lon


def assign_users_and_cards(n_rows: int, num_users: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    user_ids = rng.integers(0, num_users, size=n_rows)
    card_offsets = rng.integers(0, 2, size=n_rows)
    card_ids = user_ids * 10 + card_offsets
    return user_ids, card_ids


def _km_offset_to_latlon(lat0: np.ndarray, dist_km: np.ndarray, bearing_rad: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    dlat = (dist_km * np.cos(bearing_rad)) / KM_PER_DEGREE_LAT
    km_per_degree_lon = KM_PER_DEGREE_LAT * np.cos(np.radians(lat0))
    km_per_degree_lon = np.where(np.abs(km_per_degree_lon) < 1e-6, 1e-6, km_per_degree_lon)
    dlon = (dist_km * np.sin(bearing_rad)) / km_per_degree_lon
    return dlat, dlon


def assign_locations(
    user_ids: np.ndarray,
    class_labels: np.ndarray,
    home_lat: np.ndarray,
    home_lon: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    n = len(user_ids)
    base_lat = home_lat[user_ids]
    base_lon = home_lon[user_ids]

    far_threshold = np.where(class_labels == 1, FAR_FRACTION_FRAUD, FAR_FRACTION_LEGIT)
    is_far = rng.uniform(0.0, 1.0, size=n) < far_threshold

    near_dist_km = np.abs(rng.normal(0.0, NEAR_JITTER_KM_STD, size=n))
    far_dist_km = rng.uniform(FAR_MIN_KM, FAR_MAX_KM, size=n)
    dist_km = np.where(is_far, far_dist_km, near_dist_km)

    bearing_rad = rng.uniform(0.0, 2 * np.pi, size=n)
    dlat, dlon = _km_offset_to_latlon(base_lat, dist_km, bearing_rad)

    lat = np.clip(base_lat + dlat, -90.0, 90.0)
    lon = ((base_lon + dlon + 180.0) % 360.0) - 180.0
    return lat, lon


def assign_splits(
    class_labels: np.ndarray,
    rng: np.random.Generator,
    train_frac: float = 0.7,
    val_frac: float = 0.15,
) -> np.ndarray:
    """Stratified train/val/test split, computed once on the ORIGINAL rows
    before any replication. Every replica of a given original row must reuse
    this same array unchanged — re-rolling per replica would leak the same
    underlying transaction across splits under different synthetic identities.
    """
    n = len(class_labels)
    splits = np.empty(n, dtype=object)
    for label in np.unique(class_labels):
        idx = np.where(class_labels == label)[0]
        shuffled = rng.permutation(idx)
        n_train = int(round(len(shuffled) * train_frac))
        n_val = int(round(len(shuffled) * val_frac))
        splits[shuffled[:n_train]] = "train"
        splits[shuffled[n_train : n_train + n_val]] = "val"
        splits[shuffled[n_train + n_val :]] = "test"
    return splits
