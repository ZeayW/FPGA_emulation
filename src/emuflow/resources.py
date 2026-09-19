from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping

from .errors import ValidationError


RESOURCE_FIELDS = (
    "lut",
    "ff",
    "bram",
    "dsp",
    "carry",
    "bram18k",
    "uram288",
    "dsp48",
    "carry8",
    "io",
    "clock",
    "other",
)


@dataclass(frozen=True)
class ResourceVector:
    lut: int = 0
    ff: int = 0
    bram: int = 0
    dsp: int = 0
    carry: int = 0
    bram18k: int = 0
    uram288: int = 0
    dsp48: int = 0
    carry8: int = 0
    io: int = 0
    clock: int = 0
    other: int = 0

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], context: str = "resource vector"
    ) -> "ResourceVector":
        unknown = sorted(set(value) - set(RESOURCE_FIELDS))
        if unknown:
            raise ValidationError(
                f"{context}: unknown resource fields: {', '.join(unknown)}"
            )
        normalized: Dict[str, int] = {}
        for field in RESOURCE_FIELDS:
            raw = value.get(field, 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValidationError(
                    f"{context}.{field}: expected a non-negative integer"
                )
            normalized[field] = raw
        return cls(**normalized)

    @classmethod
    def sum(cls, vectors: Iterable["ResourceVector"]) -> "ResourceVector":
        totals = {field: 0 for field in RESOURCE_FIELDS}
        for vector in vectors:
            for field in RESOURCE_FIELDS:
                totals[field] += getattr(vector, field)
        return cls(**totals)

    def to_dict(self, include_zeros: bool = True) -> Dict[str, int]:
        result = {field: getattr(self, field) for field in RESOURCE_FIELDS}
        if include_zeros:
            return result
        return {field: count for field, count in result.items() if count}

    def fits_capacity(self, capacity: Mapping[str, int]) -> bool:
        for resource in capacity:
            if resource not in RESOURCE_FIELDS:
                raise ValidationError(
                    f"capacity: unknown resource field {resource!r}"
                )
        for resource in RESOURCE_FIELDS:
            if getattr(self, resource) > capacity.get(resource, 0):
                return False
        return True


def classify_primitive_resources(cell_type: str) -> ResourceVector:
    """Classify generic or vendor-mapped cells into planning resources."""

    kind = cell_type.upper()

    if kind.startswith("RAMB36"):
        return ResourceVector(bram18k=2)
    if kind.startswith("RAMB18"):
        return ResourceVector(bram18k=1)
    if kind in {"VTR_SP_RAM", "VTR_DP_RAM"}:
        return ResourceVector(bram=1)
    if kind in {"VTR_MULTIPLY", "MULTIPLY"}:
        return ResourceVector(dsp=1)
    if kind.startswith("URAM288"):
        return ResourceVector(uram288=1)
    if kind.startswith("DSP48"):
        return ResourceVector(dsp48=1)
    if kind.startswith("CARRY8"):
        return ResourceVector(carry8=1)
    if (
        kind.startswith("LUT")
        or kind in {"$LUT", "$_LUT_"}
        or kind.startswith("$_LUT")
    ):
        return ResourceVector(lut=1)
    if (
        kind.startswith("FD")
        or kind.startswith("$DFF")
        or kind.startswith("$SDFF")
        or kind.startswith("$ADFF")
        or kind.startswith("$_DFF")
        or kind.startswith("$_SDFF")
        or kind.startswith("$_ADFF")
    ):
        return ResourceVector(ff=1)
    if kind.startswith(("IBUF", "OBUF", "IOBUF")):
        return ResourceVector(io=1)
    if kind.startswith(("BUFG", "BUFH", "BUFMR", "MMCM", "PLLE", "STARTUPE")):
        return ResourceVector(clock=1)
    if kind in {"GND", "VCC", "MUXF7", "MUXF8", "MUXF9"}:
        return ResourceVector()
    return ResourceVector(other=1)


def classify_ultrascale_primitive(cell_type: str) -> ResourceVector:
    """Backward-compatible name for the provider-neutral classifier."""

    return classify_primitive_resources(cell_type)
