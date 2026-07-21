# Sentinel Phase 1 Baseline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the ULB credit card fraud dataset, synthetically augmented with user/card/geo fields and expanded to ~3.1M rows, loaded into a deliberately naive Postgres 16 schema, with an honest "before" benchmark of the naive per-user history query written to `BENCHMARK.md`.

**Architecture:** A monorepo (`backend/`, `infra/`, `data/`, `scripts/`) with two isolated Python venvs — one for the FastAPI backend, one for the data scripts (pandas/numpy/psycopg2). Postgres runs via Docker Compose. Two standalone Python scripts (`ingest.py`, `benchmark.py`) do all the real work; the FastAPI app is just a `/health` placeholder for this phase.

**Tech Stack:** Python 3, FastAPI + uvicorn, Postgres 16 (Docker), pandas/numpy/psycopg2-binary/requests, pytest.

## Global Constraints

- Repo: public GitHub repo `sentinel-fraud-detection` under account `rishibhansali`.
- Postgres version pinned to **16** everywhere (docker-compose, docs).
- Backend dependency management: **requirements.txt**, not Poetry.
- Migrations: **raw SQL** file, not Alembic.
- Benchmark query is **`SELECT *`** in both Phase 1 and Phase 2 — column scope must never change between "before" and "after"; only the index changes. This is a hard constraint on `benchmark.py` and on `BENCHMARK.md`'s wording.
- Cold-cache trigger is **`docker compose restart db`**, executed by the benchmark script itself, immediately before the single cold run; warm runs follow immediately after with no restart. Never substitute idle-time waiting for this.
- `data/raw/` and `data/processed/` are gitignored — never commit raw or processed dataset files.
- No scoring/detection logic, no indexes beyond the primary key, no partitioning, no streaming, no AI/Claude integration, no real frontend, no CI/CD, no API beyond `/health` — all later phases.
- **Split reservation is leakage-safe by construction:** a stratified (on `Class`) train/val/test split is assigned to each of the 284,807 *original* rows **before** the 11x replication step. Every replica generated from a given original row inherits that row's split assignment unchanged. This is what prevents the same underlying transaction from appearing in both train and test wearing a different synthetic identity. Split ratios: 70% train / 15% val / 15% test (a judgment call — flagged for confirmation, easy to adjust since it's one function argument). Persisted as a `split` column (`'train' | 'val' | 'test'`) on `transactions`. No ML training or feature engineering happens in this phase — this only reserves the split.

---

### Task 1: Repo scaffolding, GitHub repo, license, gitignore, README

**Files:**
- Create: `.gitignore`
- Create: `LICENSE`
- Create: `README.md`
- Create: `backend/`, `frontend/`, `infra/`, `data/`, `scripts/` directories (each with a `.gitkeep` if otherwise empty)

**Interfaces:**
- Produces: the directory skeleton every later task writes into.

- [ ] **Step 1: Write `.gitignore`**

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
.venv/
*.egg-info/
.pytest_cache/

# Node
node_modules/
npm-debug.log*
dist/
build/

# Data (raw + processed datasets are never committed)
data/raw/
data/processed/

# Env
.env
.env.*

# OS
.DS_Store
```

- [ ] **Step 2: Write `LICENSE`** (MIT, copyright Rishi Bhansali, 2026)

```
MIT License

Copyright (c) 2026 Rishi Bhansali

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Write `README.md`**

```markdown
# Sentinel

Real-time fraud detection and reviewer platform — a rules-based scoring
engine, a PostgreSQL optimization benchmark, and a WebSocket-driven case
review queue, built to demonstrate Postgres performance work, real-time
systems architecture, and infrastructure ownership.

**No AI layer, still works.** Every detection decision — the scoring
rules, the Postgres query optimization, the real-time event pipeline, and
the case queue — is deterministic, auditable engineering with zero AI
involvement. Claude's only role anywhere in this system (added in a later
phase) is turning an already-flagged case into a plain-English summary
after the fact — it never makes or influences a detection decision.

## Planned tech stack

- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL 16
- **Real-time:** WebSockets
- **Frontend:** React (later phase)
- **Infra:** Docker Compose locally, AWS + CI/CD later

## Status

Status: Phase 1 in progress — dataset ingestion, naive schema, and the
"before" Postgres benchmark.
```

- [ ] **Step 4: Create directory skeleton**

```bash
mkdir -p backend frontend infra data scripts
touch frontend/.gitkeep
```

- [ ] **Step 5: Commit**

```bash
git add .gitignore LICENSE README.md frontend/.gitkeep
git commit -m "Add repo scaffolding: license, gitignore, README, monorepo skeleton"
```

- [ ] **Step 6: Create the public GitHub repo and push**

```bash
gh repo create rishibhansali/sentinel-fraud-detection \
  --public \
  --description "Real-time fraud detection and reviewer platform — rules-based scoring engine, PostgreSQL optimization benchmark, WebSocket case queue." \
  --source=. --remote=origin
git push -u origin main
```

Expected: repo created at `https://github.com/rishibhansali/sentinel-fraud-detection`, all existing commits (including the two design-spec commits) pushed.

---

### Task 2: Postgres via Docker Compose + naive schema migration

**Files:**
- Create: `infra/docker-compose.yml`
- Create: `infra/migrations/001_init.sql`

**Interfaces:**
- Produces: a running `db` container reachable at `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel`, and a `transactions` table with columns `id, user_id, card_id, ts, amount, lat, lon, v1..v28, class, split` and only a primary key index. This DSN and schema are consumed by Task 5 (`ingest.py`) and Task 6 (`benchmark.py`).

- [ ] **Step 1: Write `infra/docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    container_name: sentinel-db
    environment:
      POSTGRES_DB: sentinel
      POSTGRES_USER: sentinel
      POSTGRES_PASSWORD: sentinel_dev_only
    ports:
      - "5432:5432"
    volumes:
      - sentinel_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sentinel -d sentinel"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  sentinel_pgdata:
```

- [ ] **Step 2: Start Postgres and verify it's healthy**

```bash
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps
```

Expected: `db` service status `healthy` within ~30s.

- [ ] **Step 3: Write `infra/migrations/001_init.sql`**

```sql
-- Deliberately naive Phase 1 schema: primary key only, no secondary
-- indexes, no partitioning. This is the "before" state Phase 2 optimizes.
CREATE TABLE transactions (
    id BIGINT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    v1 DOUBLE PRECISION NOT NULL,
    v2 DOUBLE PRECISION NOT NULL,
    v3 DOUBLE PRECISION NOT NULL,
    v4 DOUBLE PRECISION NOT NULL,
    v5 DOUBLE PRECISION NOT NULL,
    v6 DOUBLE PRECISION NOT NULL,
    v7 DOUBLE PRECISION NOT NULL,
    v8 DOUBLE PRECISION NOT NULL,
    v9 DOUBLE PRECISION NOT NULL,
    v10 DOUBLE PRECISION NOT NULL,
    v11 DOUBLE PRECISION NOT NULL,
    v12 DOUBLE PRECISION NOT NULL,
    v13 DOUBLE PRECISION NOT NULL,
    v14 DOUBLE PRECISION NOT NULL,
    v15 DOUBLE PRECISION NOT NULL,
    v16 DOUBLE PRECISION NOT NULL,
    v17 DOUBLE PRECISION NOT NULL,
    v18 DOUBLE PRECISION NOT NULL,
    v19 DOUBLE PRECISION NOT NULL,
    v20 DOUBLE PRECISION NOT NULL,
    v21 DOUBLE PRECISION NOT NULL,
    v22 DOUBLE PRECISION NOT NULL,
    v23 DOUBLE PRECISION NOT NULL,
    v24 DOUBLE PRECISION NOT NULL,
    v25 DOUBLE PRECISION NOT NULL,
    v26 DOUBLE PRECISION NOT NULL,
    v27 DOUBLE PRECISION NOT NULL,
    v28 DOUBLE PRECISION NOT NULL,
    class SMALLINT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('train', 'val', 'test'))
);
```

- [ ] **Step 4: Apply the migration**

```bash
psql "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel" -f infra/migrations/001_init.sql
```

- [ ] **Step 5: Verify the table and index list**

```bash
psql "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel" -c "\d transactions"
```

Expected: column list matches above; only index listed is the implicit primary-key index on `id`.

- [ ] **Step 6: Commit**

```bash
git add infra/docker-compose.yml infra/migrations/001_init.sql
git commit -m "Add Postgres 16 docker-compose service and naive transactions schema"
```

---

### Task 3: Backend FastAPI `/health` endpoint

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Produces: `app.main:app` (FastAPI instance) with `GET /health` returning `{"status": "ok"}`.

- [ ] **Step 1: Write `backend/requirements.txt`**

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
pytest==8.3.3
```

- [ ] **Step 2: Create venv and install**

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
```

- [ ] **Step 3: Write the failing test — `backend/tests/test_health.py`**

```python
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it fails**

```bash
cd backend && ../backend/.venv/bin/pytest tests/test_health.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app'`.

- [ ] **Step 5: Write `backend/app/__init__.py`** (empty file) and `backend/app/main.py`

```python
from fastapi import FastAPI

app = FastAPI(title="Sentinel")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd backend && ../backend/.venv/bin/pytest tests/test_health.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/requirements.txt backend/app/__init__.py backend/app/main.py backend/tests/test_health.py
git commit -m "Add FastAPI backend with /health endpoint"
```

---

### Task 4: Synthetic augmentation module (pure functions) + unit tests

**Files:**
- Create: `scripts/requirements.txt`
- Create: `scripts/augment.py`
- Test: `scripts/tests/test_augment.py`

**Interfaces:**
- Consumes: nothing (pure numpy functions).
- Produces (consumed by Task 5's `ingest.py`):
  - `NUM_USERS: int` constant
  - `generate_home_locations(num_users: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]` — returns `(home_lat, home_lon)`, each shape `(num_users,)`.
  - `assign_users_and_cards(n_rows: int, num_users: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]` — returns `(user_ids, card_ids)`, each shape `(n_rows,)`.
  - `assign_locations(user_ids: np.ndarray, class_labels: np.ndarray, home_lat: np.ndarray, home_lon: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]` — returns `(lat, lon)`, each shape matching `user_ids`.
  - `assign_splits(class_labels: np.ndarray, rng: np.random.Generator, train_frac: float = 0.7, val_frac: float = 0.15) -> np.ndarray` — returns an array of dtype `object` holding `"train"`/`"val"`/`"test"` strings, one per row, stratified independently within each distinct value of `class_labels`. Must be called exactly once, on the *original* (pre-replication) rows — Task 5's `ingest.py` broadcasts the same returned array to every replica of a given original row, it does not call this per replica.

- [ ] **Step 1: Write `scripts/requirements.txt`**

```
pandas==2.2.3
numpy==2.1.2
psycopg2-binary==2.9.9
requests==2.32.3
pytest==8.3.3
```

- [ ] **Step 2: Create venv and install**

```bash
python3 -m venv scripts/.venv
scripts/.venv/bin/pip install -r scripts/requirements.txt
```

- [ ] **Step 3: Write the failing tests — `scripts/tests/test_augment.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
scripts/.venv/bin/pytest scripts/tests/test_augment.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'augment'` (5 tests, all failing on collection).

- [ ] **Step 5: Write `scripts/augment.py`**

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
scripts/.venv/bin/pytest scripts/tests/test_augment.py -v
```

Expected: PASS (5 tests). If `test_assign_locations_biases_far_geo_toward_fraud` is flaky near its thresholds, increase `n` — do not loosen `FAR_FRACTION_FRAUD`/`FAR_FRACTION_LEGIT` without updating `data/README.md` to match.

- [ ] **Step 7: Commit**

```bash
git add scripts/requirements.txt scripts/augment.py scripts/tests/test_augment.py
git commit -m "Add deterministic synthetic user/card/geo augmentation and stratified split assignment"
```

---

### Task 5: Dataset acquisition + `data/README.md` + `ingest.py` (real run)

**Files:**
- Create: `data/README.md`
- Create: `scripts/ingest.py`

**Interfaces:**
- Consumes: `NUM_USERS`, `generate_home_locations`, `assign_users_and_cards`, `assign_locations`, `assign_splits` from Task 4's `scripts/augment.py`; the `transactions` table (with `split` column) from Task 2.
- Produces: a fully loaded `transactions` table (~3.1M rows, each row's `split` inherited from its original pre-replication row) that Task 6's `benchmark.py` queries.

- [ ] **Step 1: Write `data/README.md`**

```markdown
# Data augmentation notes

Source: ULB Credit Card Fraud Detection dataset (284,807 rows: `Time`,
`Amount`, `V1`-`V28` anonymized PCA features, `Class`). Downloaded from
the Google-hosted TensorFlow tutorial mirror of this exact dataset:
`https://storage.googleapis.com/download.tensorflow.org/data/creditcard.csv`.
If that mirror ever goes away, the same dataset is available from Kaggle
as `mlg-ulb/creditcardfraud` — download `creditcard.csv` manually into
`data/raw/creditcard.csv`.

**`V1`-`V28`, `Amount`, and `Class` are always the real, unmodified values
from the source dataset.** Everything else below is a documented synthetic
layer this project adds — not a claim about the original data.

## Synthetic user_id / card_id

The source dataset has no identity field at all, so any user grouping is
inherently arbitrary. `scripts/augment.py` assigns each row a `user_id` in
`[0, 5000)` and a `card_id` (`user_id * 10 + {0, 1}`, i.e. 1-2 cards per
user) using a seeded `numpy` random generator. Reproducibility comes from
a fixed seed (`42`) and fixed processing order, not from any pattern in
the real feature columns — there isn't one to use.

## Synthetic geo ("impossible geo" signal)

Each of the 5,000 synthetic users gets one seeded home `(lat, lon)`.
Per transaction:

- Normally, location is jittered tightly around home (Gaussian, ~10km std
  dev bearing-random offset).
- A biased fraction of rows instead get a location randomly placed
  500-3000km from home: **35% of `Class=1` (fraud) rows**, **3% of
  `Class=0` (legit) rows**. This bias is what makes the "impossible geo"
  signal something Phase 3's rules engine can later detect — Phase 1
  only seeds the signal, it does not do any detection.

These ratios (5,000 users, 35%/3% far-geo split, ~10km near jitter) are
documented judgment calls, not derived from any external source.

## Train/val/test split (leakage-safe by construction)

A stratified (on `Class`) 70/15/15 train/val/test split is assigned to
each of the 284,807 **original** rows — once, **before** the 11x
replication step below. Every replica generated from a given original row
inherits that row's split assignment unchanged (`scripts/augment.py`'s
`assign_splits`, called once in `ingest.py` ahead of the replica loop).

This ordering is deliberate: if the split were instead assigned per
replica (post-expansion), the same underlying transaction could land in
`train` under one synthetic identity and in `test` under another —
silent leakage that would make a later ML phase's validation numbers
untrustworthy. Persisted as a `split` column (`'train' | 'val' | 'test'`)
on `transactions`. `ingest.py` prints the exact per-split row counts (of
the 284,807 original rows) at load time — see the ingestion run output
for the authoritative counts.

## Volume expansion

The source is replicated **11x** (11 x 284,807 = 3,132,877 rows,
confirmed by `ingest.py`'s printed row count at load time) to reach a
realistic "high-write table" volume for the Phase 1/2 benchmark story.
Each replica keeps the source row's `V1`-`V28`/`Amount`/`Class` unchanged
— this is what keeps the fraud rate across the full table identical to
the source dataset's fraud rate. Each replica gets its own independently
re-rolled `user_id`/`card_id`/geo, and its transactions are shifted into
a distinct 2-day timestamp window so replicas don't collide in time.
```

- [ ] **Step 2: Write `scripts/ingest.py`**

```python
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
```

- [ ] **Step 3: Run it for real against the Task 2 Postgres instance**

```bash
scripts/.venv/bin/python scripts/ingest.py
```

Expected: prints source row count (284,807), then a final line reporting exactly 3,132,877 rows loaded. Note the exact printed count for the final report to the user.

- [ ] **Step 4: Verify row count and fraud-rate preservation directly**

```bash
psql "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel" -c \
  "SELECT count(*), sum(class), round(100.0 * sum(class) / count(*), 4) AS fraud_pct FROM transactions;"
```

Expected: `count = 3132877`; `fraud_pct` matches the source dataset's fraud rate (~0.1727%).

- [ ] **Step 4b: Verify the split is leakage-safe (each replica inherited, not re-rolled)**

```bash
psql "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel" -c \
  "SELECT split, count(*), count(*) / 11 AS per_replica FROM transactions GROUP BY split ORDER BY split;"
```

Expected: three rows (`test`, `train`, `val`); each `count(*)` is exactly
11x its `per_replica` value (11 replicas each carrying the same per-split
row count from the 284,807 original rows) — confirms no replica re-rolled
its own split. Record the `per_replica` numbers (the original-row split
counts) for the final report to the user.

- [ ] **Step 5: Re-run to confirm idempotency**

```bash
scripts/.venv/bin/python scripts/ingest.py
psql "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel" -c "SELECT count(*) FROM transactions;"
```

Expected: still exactly 3,132,877 rows (truncate-and-reload, not an append).

- [ ] **Step 6: Commit** (code and docs only — `data/raw/creditcard.csv` stays gitignored)

```bash
git add data/README.md scripts/ingest.py
git commit -m "Add dataset ingestion: augmentation, 11x volume expansion, Postgres load"
```

---

### Task 6: "Before" benchmark (cold + warm, `SELECT *`) → `BENCHMARK.md`

**Files:**
- Create: `scripts/benchmark.py`
- Create: `BENCHMARK.md` (generated by running the script, then committed)

**Interfaces:**
- Consumes: the loaded `transactions` table from Task 5; `infra/docker-compose.yml` from Task 2.
- Produces: `BENCHMARK.md` at repo root, which Phase 2 appends to.

- [ ] **Step 1: Confirm the benchmark user has enough rows to query**

```bash
psql "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel" -c \
  "SELECT user_id, count(*) FROM transactions GROUP BY user_id ORDER BY count(*) DESC LIMIT 1;"
```

Note the top `user_id` — use it as `BENCHMARK_USER_ID` in Step 2 if it differs from `1234`, so the query returns a realistic multi-row history rather than near-empty results.

- [ ] **Step 2: Write `scripts/benchmark.py`**

```python
"""Phase 1 'before' benchmark: naive per-user transaction history lookup.

Cold-cache methodology (locked in the Phase 1 design spec, not inferred
from idle time): restart the Postgres container immediately before the
single cold run, then run the warm runs back-to-back without restarting.
Column scope is locked to SELECT * in both Phase 1 and Phase 2 so the
index is the only variable that changes between "before" and "after".
"""
import statistics
import subprocess
import time
from pathlib import Path

import psycopg2

DB_DSN = "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
COMPOSE_FILE = Path(__file__).parent.parent / "infra" / "docker-compose.yml"
BENCHMARK_USER_ID = 1234  # update per Step 1's result if needed
QUERY_LIMIT = 50
WARM_RUNS = 20
READINESS_TIMEOUT_S = 60

QUERY = "SELECT * FROM transactions WHERE user_id = %s ORDER BY ts DESC LIMIT %s;"


def restart_db_container() -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "restart", "db"], check=True
    )
    deadline = time.monotonic() + READINESS_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(DB_DSN, connect_timeout=2)
            conn.close()
            return
        except psycopg2.OperationalError:
            time.sleep(1)
    raise RuntimeError("Postgres did not become ready after restart")


def run_query_once() -> float:
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            start = time.perf_counter()
            cur.execute(QUERY, (BENCHMARK_USER_ID, QUERY_LIMIT))
            cur.fetchall()
            return time.perf_counter() - start
    finally:
        conn.close()


def main() -> None:
    print("Restarting Postgres container for cold-cache run...")
    restart_db_container()

    cold_seconds = run_query_once()
    print(f"Cold run: {cold_seconds * 1000:.2f} ms")

    warm_seconds = [run_query_once() for _ in range(WARM_RUNS)]
    warm_ms = sorted(s * 1000 for s in warm_seconds)
    median_ms = statistics.median(warm_ms)
    p95_ms = warm_ms[int(len(warm_ms) * 0.95) - 1]

    report = f"""# BENCHMARK.md

## Phase 1 — "before" baseline

Query (locked for apples-to-apples comparison with Phase 2 — column scope
does not change, only the index does):

```sql
{QUERY.strip()}
```

Run against the full augmented `transactions` table (~3.1M rows; see
`data/README.md` for exact row count and augmentation method). Schema has
no secondary index on `user_id`/`ts` — primary key only (see
`infra/migrations/001_init.sql`).

### Methodology

Cold cache is triggered explicitly, not inferred from idle time:

1. `docker compose -f infra/docker-compose.yml restart db` — drops the
   container's OS page cache and Postgres shared_buffers.
2. The first query after the container reports ready is the recorded
   **cold** run.
3. {WARM_RUNS} further queries run back-to-back afterward, without any
   restart — these are the recorded **warm** runs.

### Results

- Cold run: {cold_seconds * 1000:.2f} ms
- Warm runs (n={WARM_RUNS}): median {median_ms:.2f} ms, p95 {p95_ms:.2f} ms

Phase 2 appends its "after" numbers below this line using the same query
and the same restart-based cold-cache procedure.
"""
    (Path(__file__).parent.parent / "BENCHMARK.md").write_text(report)
    print(f"Warm median: {median_ms:.2f} ms, warm p95: {p95_ms:.2f} ms")
    print("Wrote BENCHMARK.md")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run it for real**

```bash
scripts/.venv/bin/python scripts/benchmark.py
```

Expected: prints cold run ms, then warm median/p95 ms, then "Wrote BENCHMARK.md". A slow cold run and a slow-ish warm run (no index on `user_id`) is the correct, expected outcome — do not treat it as a bug.

- [ ] **Step 4: Read `BENCHMARK.md` and sanity-check it**

Confirm it states the locked `SELECT *` query, the exact restart-based methodology, and real numbers (not placeholders).

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark.py BENCHMARK.md
git commit -m "Add before-benchmark script and record naive per-user query baseline"
git push
```

---

## Self-Review Notes

- **Spec coverage:** repo/GitHub setup (Task 1), monorepo + docker-compose + backend (Tasks 1-3), naive schema including `split` column (Task 2), dataset acquisition + synthetic augmentation + leakage-safe pre-replication split reservation + volume expansion + ingestion (Tasks 4-5), before-benchmark with locked `SELECT *` and explicit cold-cache restart (Task 6). All in-scope spec sections are covered, including the split-reservation requirement; all out-of-scope items (indexing, scoring, streaming, WebSocket API, Claude, frontend, CI/CD, ML training) are untouched.
- **Type/interface consistency:** `augment.py`'s four functions (`generate_home_locations`, `assign_users_and_cards`, `assign_locations`, `assign_splits`) and `NUM_USERS` are used with identical signatures in both `test_augment.py` (Task 4) and `ingest.py` (Task 5). Column names produced by `build_augmented_frame` (`id, user_id, card_id, ts, amount, lat, lon, v1..v28, class, split`) match the schema in `001_init.sql` (Task 2) exactly, including order. `assign_splits` is called exactly once per ingest run, on the pre-replication source frame, with its own RNG instance — the replica loop only reads its output, never recomputes it.
- **No placeholders:** all code blocks are complete and runnable given a working Docker + Python 3 environment and network access to the dataset mirror.
