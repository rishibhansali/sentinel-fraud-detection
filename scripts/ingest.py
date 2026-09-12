"""Idempotent (truncate-and-reload) loader: ULB creditcard fraud dataset
-> synthetically augmented -> Postgres `transactions` table.

See data/README.md for why and how the synthetic fields are generated.
V1-V28, amount, and class are always copied verbatim from the source CSV.
"""
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).parent))
from augment import (
    NUM_USERS,
    assign_locations,
    assign_splits,
    assign_users_and_cards,
    generate_home_locations,
)

DATA_DIR = Path(__file__).parent.parent / "data"
RAW_CSV = DATA_DIR / "raw" / "creditcard.csv"
DATASET_URL = "https://storage.googleapis.com/download.tensorflow.org/data/creditcard.csv"
NUM_REPLICAS = 11
SEED = 42
BASE_TIMESTAMP = datetime(2025, 1, 1, tzinfo=timezone.utc)
REPLICA_WINDOW = timedelta(days=2)
DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
V_COLUMNS = [f"V{i}" for i in range(1, 29)]


def download_raw_csv() -> None:
    if RAW_CSV.exists():
        return
    RAW_CSV.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading source dataset from {DATASET_URL} ...")
    response = requests.get(DATASET_URL, timeout=60)
    response.raise_for_status()
    RAW_CSV.write_bytes(response.content)
    print(f"Saved {len(response.content):,} bytes to {RAW_CSV}")


def build_augmented_frame(source: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    home_lat, home_lon = generate_home_locations(NUM_USERS, rng)

    # Split is assigned ONCE on the original rows, before replication, using
    # its own RNG instance so it never competes with the augmentation draws
    # above for random state. Every replica below reuses this exact array.
    split_rng = np.random.default_rng(SEED)
    split_labels = assign_splits(source["Class"].to_numpy(), split_rng)
    unique, counts = np.unique(split_labels, return_counts=True)
    split_summary = dict(zip(unique.tolist(), counts.tolist()))
    print(
        f"Split assignment (stratified on Class, pre-replication, "
        f"{len(source):,} original rows): {split_summary}"
    )

    n_source = len(source)
    replicas = []
    next_id = 1
    for replica_index in range(NUM_REPLICAS):
        replica = source.copy()
        user_ids, card_ids = assign_users_and_cards(n_source, NUM_USERS, rng)
        lat, lon = assign_locations(
            user_ids, replica["Class"].to_numpy(), home_lat, home_lon, rng
        )
        replica_start = BASE_TIMESTAMP + replica_index * REPLICA_WINDOW
        offsets_seconds = replica["Time"].to_numpy(dtype="float64")
        offsets_seconds = offsets_seconds - offsets_seconds.min()
        timestamps = [replica_start + timedelta(seconds=float(s)) for s in offsets_seconds]

        replica["id"] = np.arange(next_id, next_id + n_source)
        replica["user_id"] = user_ids
        replica["card_id"] = card_ids
        replica["ts"] = timestamps
        replica["lat"] = lat
        replica["lon"] = lon
        replica["split"] = split_labels  # inherited from the original row, not re-rolled
        next_id += n_source
        replicas.append(replica)

    augmented = pd.concat(replicas, ignore_index=True)
    columns = ["id", "user_id", "card_id", "ts", "Amount", "lat", "lon"] + V_COLUMNS + ["Class", "split"]
    augmented = augmented[columns].rename(
        columns={"Amount": "amount", "Class": "class", **{v: v.lower() for v in V_COLUMNS}}
    )
    return augmented


def load_to_postgres(df: pd.DataFrame) -> None:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, header=False)
    buffer.seek(0)

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE transactions;")
            columns = ", ".join(df.columns)
            cur.copy_expert(
                f"COPY transactions ({columns}) FROM STDIN WITH (FORMAT csv)", buffer
            )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    download_raw_csv()
    source = pd.read_csv(RAW_CSV)
    print(f"Loaded {len(source):,} source rows from {RAW_CSV}")

    augmented = build_augmented_frame(source)
    load_to_postgres(augmented)

    print(
        f"Loaded {len(augmented):,} rows into transactions "
        f"({NUM_REPLICAS} replicas x {len(source):,} source rows, "
        f"NUM_USERS={NUM_USERS}, SEED={SEED})"
    )


if __name__ == "__main__":
    main()
