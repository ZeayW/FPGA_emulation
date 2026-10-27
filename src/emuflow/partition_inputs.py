"""Small shared partition identity and net-weight readers."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from .errors import ValidationError
from .io import read_json
from .sta import PARTITION_NET_WEIGHTS_INDEX_SCHEMA, load_partition_net_weights_indexed

PARTITION_NET_WEIGHTS_SCHEMA = "emuflow.partition-net-weights/v1"


class _CompactClusterOrder(list):
    """Canonical cluster identities without one Python string per vertex."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[str]:
        for index in range(self.count):
            yield f"c{index:06d}"

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self.count))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("compact cluster index must be an integer")
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError("compact cluster index out of range")
        return f"c{index:06d}"


def load_partition_net_weights(path: Optional[Path]) -> Dict[str, float]:
    if path is None:
        return {}
    value = read_json(path)
    if value.get("schema") == PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        return load_partition_net_weights_indexed(path)
    if value.get("schema") != PARTITION_NET_WEIGHTS_SCHEMA:
        raise ValidationError(
            "net weights schema: expected "
            f"{PARTITION_NET_WEIGHTS_SCHEMA!r}, got {value.get('schema')!r}"
        )
    raw_weights = value.get("weights")
    if not isinstance(raw_weights, dict):
        raise ValidationError("net weights.weights: expected an object")
    weights: Dict[str, float] = {}
    for net_id, raw_weight in raw_weights.items():
        if not isinstance(net_id, str) or not net_id:
            raise ValidationError(
                "net weights.weights: keys must be non-empty strings"
            )
        if (
            isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or not math.isfinite(float(raw_weight))
            or float(raw_weight) <= 0.0
        ):
            raise ValidationError(
                f"net weights.weights[{net_id!r}]: expected a positive number"
            )
        weights[net_id] = float(raw_weight)
    return weights
