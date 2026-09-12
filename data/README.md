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
