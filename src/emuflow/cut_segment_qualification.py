"""Pre-route qualification for partition cut logic segments.

OpenSTA 2.6 cannot reliably enumerate reconvergent sibling paths through an
internal net: both ``-through`` and an internal ``-from`` constraint may return
the unrelated worst sibling.  This module therefore keeps functional cut
coverage independent of provider path enumeration.  It rebuilds reachability
from EmuIR and the timing-cell contract, associates any enumerated original
TimingPathDB members, and binds the static-exact segment identities.  Routed
endpoint-exact delay remains a mandatory Phase 7 qualification gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import read_json
from .ir import EmuIR
from .opensta import (
    DEFAULT_TIMING_MODEL,
    build_vtr_opensta_timing_model,
    classify_through_net_timing_endpoints,
    load_timing_model,
)
from .sta import validate_sta_path_database_value


CUT_SEGMENT_QUALIFICATION_SCHEMA = (
    "emuflow.cut-segment-timing-qualification/v1"
)
CUT_SEGMENT_QUALIFICATION_PROVIDER = (
    "emuir-structural-cut-segment-qualification-v1"
)


def _cut_nets(assignment: Mapping[str, Any]) -> list[str]:
    raw = assignment.get("cut_nets")
    if not isinstance(raw, list):
        raise ValidationError("cut-segment qualification cut_nets is invalid")
    nets = [
        item.get("net")
        for item in raw
        if isinstance(item, dict)
    ]
    if (
        not nets
        or any(not isinstance(net, str) or not net for net in nets)
        or len(nets) != len(set(nets))
    ):
        raise ValidationError(
            "cut-segment qualification requires unique partition cut nets"
        )
    return sorted(nets)


def _segment_ids_by_cut(
    assignment: Mapping[str, Any], cut_nets: list[str]
) -> Dict[str, Dict[str, list[str]]]:
    result = {
        net: {"source_segment_ids": [], "capture_segment_ids": []}
        for net in cut_nets
    }
    contract = assignment.get("semantic_contract")
    if contract is None:
        return result
    if not isinstance(contract, dict):
        raise ValidationError(
            "cut-segment qualification semantic contract is invalid"
        )
    nodes = contract.get("cut_nodes")
    if not isinstance(nodes, list):
        raise ValidationError(
            "cut-segment qualification cut-node contract is invalid"
        )
    seen = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("net") not in result:
            raise ValidationError(
                "cut-segment qualification cut-node set is invalid"
            )
        net = node["net"]
        if net in seen:
            raise ValidationError(
                "cut-segment qualification cut-node IDs are not unique"
            )
        seen.add(net)
        for field in ("source_segment_ids", "capture_segment_ids"):
            values = node.get(field)
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise ValidationError(
                    f"cut-segment qualification {field} is invalid"
                )
            result[net][field] = sorted(values)
    if seen != set(cut_nets):
        raise ValidationError(
            "cut-segment qualification cut-node coverage is incomplete"
        )
    return result


def build_cut_segment_qualification(
    ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    *,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build deterministic pre-route cut qualification without STA guessing."""

    ir = EmuIR.load(ir_path)
    assignment = read_json(assignment_path)
    database = read_json(path_database_path)
    validate_sta_path_database_value(database, ir)
    return build_cut_segment_qualification_value(
        ir,
        assignment,
        database,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
    )


def build_cut_segment_qualification_value(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    database: Mapping[str, Any],
    *,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build qualification from one validated in-memory input set."""

    cut_nets = _cut_nets(assignment)
    segment_ids = _segment_ids_by_cut(assignment, cut_nets)
    if architecture_timing_db_path is not None:
        model, instance_cell_types = build_vtr_opensta_timing_model(
            ir, architecture_timing_db_path
        )
    else:
        model = load_timing_model(timing_model_path)
        instance_cell_types = None
    structural = classify_through_net_timing_endpoints(
        ir, model, cut_nets, instance_cell_types
    )
    member_ids: Dict[str, list[str]] = {net: [] for net in cut_nets}
    for path in database.get("paths", []):
        if not isinstance(path, dict) or not isinstance(path.get("path_nets"), list):
            raise ValidationError(
                "cut-segment qualification original path record is invalid"
            )
        path_id = path.get("id")
        if not isinstance(path_id, str) or not path_id:
            raise ValidationError(
                "cut-segment qualification original path ID is invalid"
            )
        for net in set(path["path_nets"]) & set(cut_nets):
            member_ids[net].append(path_id)
    net_by_id = {net["id"]: net for net in ir.value["nets"]}
    records = []
    for net in cut_nets:
        if net not in net_by_id:
            raise ValidationError(
                f"cut-segment qualification net is absent from EmuIR: {net!r}"
            )
        drivers = net_by_id[net].get("drivers")
        if not isinstance(drivers, list) or len(drivers) != 1:
            raise ValidationError(
                f"cut-segment qualification net must have one driver: {net!r}"
            )
        timed = structural[net]["status"] == "timed"
        members = sorted(set(member_ids[net]))
        records.append(
            {
                "net": net,
                "driver_count": 1,
                "classification": (
                    "timed-structural" if timed else "no-timed-endpoint"
                ),
                "structural": structural[net],
                "associated_original_path_ids": members,
                "original_path_association": (
                    "enumerated-members" if members else "functional-only"
                ),
                **segment_ids[net],
            }
        )
    return {
        "schema": CUT_SEGMENT_QUALIFICATION_SCHEMA,
        "status": "pass",
        "provider": CUT_SEGMENT_QUALIFICATION_PROVIDER,
        "qualification": "pre-route-structural-contract-bound",
        "delay_evidence": "contract-budget-provisional",
        "routed_deadline_gate": "required-phase7-endpoint-exact-or-conservative-bound",
        "cut_nets": cut_nets,
        "records": records,
        "summary": {
            "cut_nets": len(records),
            "timed_structural_nets": sum(
                record["classification"] == "timed-structural"
                for record in records
            ),
            "no_timed_endpoint_nets": sum(
                record["classification"] == "no-timed-endpoint"
                for record in records
            ),
            "enumerated_member_associations": sum(
                len(record["associated_original_path_ids"])
                for record in records
            ),
            "functional_only_nets": sum(
                record["original_path_association"] == "functional-only"
                for record in records
            ),
        },
    }


def validate_cut_segment_qualification(
    artifact: Mapping[str, Any],
    ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    *,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    ir = EmuIR.load(ir_path)
    assignment = read_json(assignment_path)
    database = read_json(path_database_path)
    validate_sta_path_database_value(database, ir)
    expected = build_cut_segment_qualification_value(
        ir,
        assignment,
        database,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
    )
    if dict(artifact) != expected:
        raise ValidationError(
            "cut-segment qualification is not independently reconstructed"
        )
    return {
        "status": "pass",
        **expected["summary"],
    }
