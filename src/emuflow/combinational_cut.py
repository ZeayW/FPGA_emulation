"""Current Static Exact policy, canonical identities and typed candidate views."""
from __future__ import annotations
import hashlib
import json
from array import array
from collections.abc import Set as AbstractSet
from typing import Any, Dict, Iterable, Iterator, List, Mapping
from .errors import ValidationError
from .phase3_exact_records import direct_static_exact_semantic_contract_sha256


STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA = (
    "emuflow.static-exact-combinational-cut/v3"
)


STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS = {
    STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
}


STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2 = "assignment-derived-acyclic-v2"


STATIC_EXACT_DEFAULT_CANDIDATE_POLICY = STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2


STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH = 0


STATIC_EXACT_CANDIDATE_POLICIES = {
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
}


SEQUENTIAL_TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}


SEQUENTIAL_LEGAL_CUT_CLASSES = {
    *SEQUENTIAL_TRANSPORTED_CUT_CLASSES,
    "primary_input",
}


REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}


SLOT_EDGE_CONVENTION = {
    "id": "tx-sample-before-rx-shadow-update-v1",
    "tx_sample": (
        "a TX assigned slot S samples its source at the fabric rising edge "
        "for which the pre-edge controller value is S"
    ),
    "rx_capture": (
        "an RX assigned arrival slot A updates its shadow register at the "
        "fabric rising edge for which the pre-edge controller value is A; "
        "a TX sampling on that edge observes the pre-update shadow value"
    ),
    "settle_budget": (
        "a value captured or launched at edge E with budget B is first "
        "eligible for downstream sampling at edge E+B"
    ),
    "relay_constraint": "next_tx_slot >= arrival_slot + settle_budget_slots",
    "commit": (
        "the virtual DUT commits at the rising edge whose pre-edge slot is "
        "frame_slots-1; capture-ready may equal that commit slot"
    ),
}


_STATE_RESOURCES = {"ff", "bram", "bram18k", "uram288"}


_HARD_COMBINATIONAL_RESOURCES = {"dsp", "carry", "dsp48", "carry8"}


_TRANSPORT_SAFE_SEQUENTIAL_INPUTS = {
    "FDCE": {"D", "CE"},
    "FDPE": {"D", "CE"},
    "FDRE": {"D", "CE", "R"},
    "FDSE": {"D", "CE", "S"},
}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    # Keep the byte identity of ``json.dumps(..., indent=2, sort_keys=True)``
    # without materializing a second, potentially multi-gigabyte string for a
    # production EmuIR.  JSONEncoder.iterencode preserves the same canonical
    # token stream while bounding the additional hashing memory to one chunk.
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def semantic_contract_sha256(value: Mapping[str, Any]) -> str:
    """Return the canonical identity propagated across exact-mode stages."""

    direct = direct_static_exact_semantic_contract_sha256(value)
    return direct if direct is not None else _canonical_sha256(value)


class _PackedEligibleCutIds(AbstractSet[str]):
    """Self-contained ordinal bitmap plus lexical UTF-8 identity pool."""

    __slots__ = ("_ordinal_bits", "_name_bytes", "_name_offsets", "_net_count")

    def __init__(
        self,
        ordinal_bits: bytearray,
        name_bytes: bytes,
        name_offsets: array,
        net_count: int,
    ) -> None:
        if len(ordinal_bits) != (net_count + 7) // 8:
            raise ValidationError("compact eligible-cut bitmap is invalid")
        if (
            name_offsets.typecode != "Q"
            or not name_offsets
            or name_offsets[0] != 0
            or name_offsets[-1] != len(name_bytes)
            or any(
                name_offsets[index - 1] > name_offsets[index]
                for index in range(1, len(name_offsets))
            )
        ):
            raise ValidationError("compact eligible-cut name pool is invalid")
        self._ordinal_bits = bytes(ordinal_bits)
        self._name_bytes = name_bytes
        self._name_offsets = name_offsets
        self._net_count = net_count

    def __len__(self) -> int:
        return len(self._name_offsets) - 1

    def __iter__(self) -> Iterator[str]:
        for index in range(len(self)):
            yield self.name_at(index)

    def name_at(self, index: int) -> str:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("packed eligible-cut name index out of range")
        return self._name_bytes[
            self._name_offsets[index] : self._name_offsets[index + 1]
        ].decode("utf-8")

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        encoded = value.encode("utf-8")
        lower = 0
        upper = len(self)
        while lower < upper:
            middle = (lower + upper) // 2
            candidate = self._name_bytes[
                self._name_offsets[middle] : self._name_offsets[middle + 1]
            ]
            if candidate < encoded:
                lower = middle + 1
            else:
                upper = middle
        return (
            lower < len(self)
            and self._name_bytes[
                self._name_offsets[lower] : self._name_offsets[lower + 1]
            ]
            == encoded
        )

    def contains_ordinal(self, ordinal: int) -> bool:
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal >= self._net_count
        ):
            return False
        return bool(
            self._ordinal_bits[ordinal >> 3] & (1 << (ordinal & 7))
        )

    @property
    def packed_bytes(self) -> int:
        return (
            len(self._ordinal_bits)
            + len(self._name_bytes)
            + len(self._name_offsets) * self._name_offsets.itemsize
        )

    def ordered_names(self) -> List[str]:
        return list(self)

    def streaming_ordered_names(self) -> List[str]:
        return _PackedEligibleCutNames(self)


class _PackedEligibleCutNames(list):
    """List-compatible lazy view over packed eligible-cut identities.

    The Phase 3 cluster schema still exposes an ordered JSON array. Keeping a
    normal Python list until publication would duplicate every decoded net
    name after compact candidate construction. JSON encoders iterate list
    subclasses, so this view preserves the existing schema and exact bytes
    while decoding each name only as it is consumed. Explicit mutation first
    materializes an ordinary backing list, preserving compatibility for
    callers that intentionally edit a cluster artifact.
    """

    __slots__ = ("_source", "_materialized")

    def __init__(self, source: _PackedEligibleCutIds) -> None:
        super().__init__()
        self._source = source
        self._materialized = False

    def _ensure_materialized(self) -> None:
        if self._materialized:
            return
        list.extend(self, self._source)
        self._materialized = True

    def __len__(self) -> int:
        if self._materialized:
            return list.__len__(self)
        return len(self._source)

    def __bool__(self) -> bool:
        return len(self) != 0

    def __iter__(self) -> Iterator[str]:
        if self._materialized:
            return list.__iter__(self)
        return iter(self._source)

    def __reversed__(self) -> Iterator[str]:
        if self._materialized:
            return list.__reversed__(self)
        return (
            self._source.name_at(index)
            for index in range(len(self._source) - 1, -1, -1)
        )

    def __getitem__(self, index: Any) -> Any:
        if self._materialized:
            return list.__getitem__(self, index)
        if isinstance(index, slice):
            return [
                self._source.name_at(item)
                for item in range(*index.indices(len(self)))
            ]
        return self._source.name_at(index)

    def __contains__(self, value: object) -> bool:
        if self._materialized:
            return list.__contains__(self, value)
        return value in self._source

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, list):
            return False
        if len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __lt__(self, other: object) -> bool:
        return list(self) < other

    def __le__(self, other: object) -> bool:
        return list(self) <= other

    def __gt__(self, other: object) -> bool:
        return list(self) > other

    def __ge__(self, other: object) -> bool:
        return list(self) >= other

    def __add__(self, other: object) -> List[Any]:
        return list(self) + other

    def __radd__(self, other: object) -> List[Any]:
        return other + list(self)

    def __mul__(self, count: int) -> List[str]:
        return list(self) * count

    def __rmul__(self, count: int) -> List[str]:
        return list(self) * count

    def __repr__(self) -> str:
        return repr(list(self))

    def copy(self) -> List[str]:
        return list(self)

    def __copy__(self) -> List[str]:
        return list(self)

    def __deepcopy__(self, memo: Dict[int, Any]) -> List[str]:
        result = list(self)
        memo[id(self)] = result
        return result

    def __reduce_ex__(self, protocol: int) -> Any:
        return (list, (list(self),))

    def __setitem__(self, index: Any, value: Any) -> None:
        self._ensure_materialized()
        list.__setitem__(self, index, value)

    def __delitem__(self, index: Any) -> None:
        self._ensure_materialized()
        list.__delitem__(self, index)

    def __iadd__(self, values: Iterable[Any]) -> "_PackedEligibleCutNames":
        self._ensure_materialized()
        list.__iadd__(self, values)
        return self

    def __imul__(self, count: int) -> "_PackedEligibleCutNames":
        self._ensure_materialized()
        list.__imul__(self, count)
        return self

    def append(self, value: Any) -> None:
        self._ensure_materialized()
        list.append(self, value)

    def extend(self, values: Iterable[Any]) -> None:
        self._ensure_materialized()
        list.extend(self, values)

    def insert(self, index: int, value: Any) -> None:
        self._ensure_materialized()
        list.insert(self, index, value)

    def pop(self, index: int = -1) -> Any:
        self._ensure_materialized()
        return list.pop(self, index)

    def remove(self, value: Any) -> None:
        self._ensure_materialized()
        list.remove(self, value)

    def clear(self) -> None:
        self._ensure_materialized()
        list.clear(self)

    def reverse(self) -> None:
        self._ensure_materialized()
        list.reverse(self)

    def sort(self, *args: Any, **kwargs: Any) -> None:
        self._ensure_materialized()
        list.sort(self, *args, **kwargs)

    def count(self, value: Any) -> int:
        if self._materialized:
            return list.count(self, value)
        return sum(item == value for item in self._source)

    def index(
        self, value: Any, start: int = 0, stop: int | None = None
    ) -> int:
        values = list(self)
        if stop is None:
            return values.index(value, start)
        return values.index(value, start, stop)
