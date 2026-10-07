"""Read the untouched ULB CSV and reproduce ingest's original-row split."""

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.ml.features import FEATURE_NAMES, extract_features

from augment import assign_splits
from ingest import SEED

SOURCE_COLUMNS = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount", "Class"]


@dataclass(frozen=True)
class SourceData:
    features: np.ndarray
    labels: np.ndarray
    splits: np.ndarray
    source_row_indices: np.ndarray
    source_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_source_csv(path: Path, expected_rows: int = 284_807) -> SourceData:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Source CSV not found: {path}. See data/README.md for acquisition.")
    try:
        frame = pd.read_csv(path)
    except pd.errors.ParserError as exc:
        raise ValueError(f"Malformed source CSV: {exc}") from exc
    if list(frame.columns) != SOURCE_COLUMNS:
        raise ValueError("Source CSV must have exactly Time, V1–V28, Amount, Class in order")
    if len(frame) != expected_rows:
        raise ValueError(f"Source CSV has {len(frame)} rows; expected {expected_rows}")
    try:
        labels = pd.to_numeric(frame["Class"], errors="raise").to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Class must contain only binary 0/1 labels") from exc
    if not np.isin(labels, (0, 1)).all():
        raise ValueError("Class must contain only binary 0/1 labels")
    try:
        features = np.asarray(
            [extract_features(row[1:29], row[29]) for row in frame.itertuples(index=False, name=None)],
            dtype=np.float64,
        )
    except ValueError as exc:
        raise ValueError(f"Invalid model input in source CSV: {exc}") from exc
    if features.shape != (expected_rows, len(FEATURE_NAMES)):
        raise ValueError("Unexpected feature matrix shape")
    labels = labels.astype(np.int8)
    splits = assign_splits(labels, np.random.default_rng(SEED))
    indices = np.arange(expected_rows, dtype=np.int64)
    masks = [splits == name for name in ("train", "val", "test")]
    if not np.all(np.sum(masks, axis=0) == 1):
        raise ValueError("Original source rows must belong to exactly one split")
    return SourceData(features, labels, splits, indices, _sha256(path))
