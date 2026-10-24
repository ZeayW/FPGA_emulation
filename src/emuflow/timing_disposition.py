"""Original-path coverage and numerical eligibility after physical optimization."""
from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import ValidationError

OPTIMIZED_AWAY = "optimized-away"
PATH_POPULATION_POLICY = "implemented-original-paths-with-explicit-optimization-dispositions-v1"
_SOURCE = re.compile(r"(?:s(?:0|[1-9][0-9]*)|p(?:[0-9a-f]{2})+:(?:0|[1-9][0-9]*))\Z")


def is_optimized_away(record: Mapping[str, Any]) -> bool:
    return record.get("measurement") == OPTIMIZED_AWAY


def validate_optimization_record(record: Mapping[str, Any]) -> bool:
    """Check a disposition reference, without inventing a physical delay.

    The physical summary owns proof artifacts and validates the referenced
    fact. This record-level check cannot certify an external proof on its own.
    """
    if not is_optimized_away(record):
        if "optimization_source" in record:
            raise ValidationError("measured timing record carries an elimination reference")
        return False
    source = record.get("optimization_source")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise ValidationError("optimized-away timing record lacks a valid source proof reference")
    if record.get("delay_ns") is not None or any(record.get(key) is not None for key in (
        "actual_start_object", "actual_end_object", "start_object", "end_object", "arrival_ns", "slack_ns",
        "system_delay_bound_ns", "target_clock_slack_bound_ns", "runtime_clock_slack_bound_ns",
    )):
        raise ValidationError("optimized-away timing record must not have numerical delay, slack, or physical endpoints")
    return True


def require_optimization_proof(physical: Mapping[str, Any], fpga: str, source: str,
                               record: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Require the canonical partition proof, including for every global skip."""
    proofs = physical.get("optimization_proofs", {})
    view = proofs.get(fpga) if isinstance(proofs, Mapping) else None
    require = getattr(view, "require_eliminated_source", None)
    if not callable(require):
        raise ValidationError("optimized-away timing requires its partition source proof owner")
    proof = require(source)
    if record is not None:
        cells = {str(record.get(key, "")).rpartition("/")[0].replace("\\\\", "\\")
                 for key in ("start_pin", "end_pin")}
        if proof.get("source_cell") not in cells:
            raise ValidationError("timing elimination proof belongs to a different source endpoint")
    return proof


def require_boundary_optimization_proof(physical: Mapping[str, Any], fpga: str,
                                        record: Mapping[str, Any], identity: Mapping[str, Any]) -> Mapping[str, Any]:
    """Bind a removed interface to its canonical Phase6 shadow cell."""
    proof = require_optimization_proof(physical, fpga, record["optimization_source"])
    cells = identity.get("merged_ir", {}).get("boundary_register_instances", ())
    if proof["source_cell"] not in cells:
        raise ValidationError("boundary elimination proof belongs to a different source endpoint")
    return proof
