import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from augment import assign_splits
from ml_dataset import load_source_csv


def make_source(path: Path, count: int = 400) -> pd.DataFrame:
    rows = {"Time": np.arange(count, dtype=float)}
    rows.update({f"V{i}": np.arange(count, dtype=float) / (i + 1) for i in range(1, 29)})
    rows["Amount"] = np.arange(count, dtype=float)
    rows["Class"] = np.arange(count) % 10 == 0
    frame = pd.DataFrame(rows)
    frame.to_csv(path, index=False)
    return frame


def test_original_rows_split_once_and_class_is_not_a_feature(tmp_path):
    path = tmp_path / "creditcard.csv"
    frame = make_source(path)
    data = load_source_csv(path, expected_rows=400)
    assert data.features.shape == (400, 29)
    assert np.array_equal(data.source_row_indices, np.arange(400))
    assert np.array_equal(data.splits, assign_splits(data.labels, np.random.default_rng(42)))
    assert set(data.splits) == {"train", "val", "test"}
    assert sum((data.splits == name).sum() for name in ("train", "val", "test")) == 400
    assert data.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    frame["Class"] = 1 - frame["Class"].astype(int)
    frame.to_csv(path, index=False)
    changed = load_source_csv(path, expected_rows=400)
    assert np.array_equal(data.features, changed.features)
    assert not np.array_equal(data.labels, changed.labels)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda frame: frame.iloc[:-1],
        lambda frame: frame.assign(extra=1),
        lambda frame: frame.drop(columns=["V28"]),
        lambda frame: frame.assign(Amount=-1),
        lambda frame: frame.assign(Class=2),
        lambda frame: frame.assign(V1=float("nan")),
        lambda frame: frame.assign(V2=float("inf")),
    ],
)
def test_invalid_source_fails(tmp_path, mutation):
    path = tmp_path / "creditcard.csv"
    frame = make_source(path)
    mutation(frame).to_csv(path, index=False)
    with pytest.raises(ValueError):
        load_source_csv(path, expected_rows=400)


def test_missing_source_points_to_acquisition_notes(tmp_path):
    with pytest.raises(FileNotFoundError, match="data/README.md"):
        load_source_csv(tmp_path / "missing.csv", expected_rows=400)
