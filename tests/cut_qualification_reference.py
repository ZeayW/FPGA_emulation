"""Independent test-only structural cut qualification.

OpenSTA 2.6 cannot reliably enumerate reconvergent sibling paths through an
internal net: both ``-through`` and an internal ``-from`` constraint may return
the unrelated worst sibling.  This module therefore keeps functional cut
coverage independent of provider path enumeration.  It rebuilds reachability
from EmuIR and the timing-cell contract, associates any enumerated original
TimingPathDB members, and binds the static-exact segment identities.  Routed
endpoint-exact delay remains a mandatory Phase 7 qualification gate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.ir.ir import EmuIR
from emuflow.timing.opensta import DEFAULT_TIMING_MODEL, build_architecture_opensta_timing_model, classify_through_net_timing_endpoints, load_timing_model
from emuflow.partition.phase3_assignment_index import phase3_exact_relation_counts
from emuflow.partition.phase3_storage import read_phase3_cut_timing_view
from emuflow.timing.sta import iter_sta_path_database_paths, validate_sta_path_database_path_value


CUT_SEGMENT_QUALIFICATION_SCHEMA = (
    "emuflow.cut-segment-timing-qualification/v1"
)
CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA = (
    "emuflow.cut-segment-timing-qualification/v2"
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
        any(not isinstance(net, str) or not net for net in nets)
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
    contract = assignment.get(
        "semantic_contract_index", assignment.get("semantic_contract")
    )
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


def _sealed_rows(rows: Iterable[Any]) -> Dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = json.dumps(
            row, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


def _build_indexed_cut_segment_qualification(
    ir,
    assignment: Mapping[str, Any],
    database: Mapping[str, Any],
    *,
    assignment_index_path: Path,
    timing_model_path: Path,
    architecture_timing_db_path: Optional[Path],
    path_records: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build bounded qualification evidence from the Phase-3 data plane."""

    cut_nets = _cut_nets(assignment)
    if architecture_timing_db_path is not None:
        model, instance_cell_types = build_architecture_opensta_timing_model(
            ir, architecture_timing_db_path
        )
    else:
        model = load_timing_model(timing_model_path)
        instance_cell_types = None
    structural = classify_through_net_timing_endpoints(
        ir, model, cut_nets, instance_cell_types
    )

    alias = "cut_timing_assignment"
    ir.connection.execute(
        f"ATTACH DATABASE ? AS {alias}",
        (immutable_sqlite_uri(assignment_index_path),),
    )
    try:
        unknown = int(
            ir.connection.execute(
                f"""SELECT COUNT(*) FROM (
                  SELECT DISTINCT c.net_id FROM {alias}.cut_sinks c
                  LEFT JOIN net_records n ON n.id=c.net_id
                  WHERE n.seq IS NULL
                )"""
            ).fetchone()[0]
        )
        invalid_drivers = int(
            ir.connection.execute(
                f"""SELECT COUNT(*) FROM (
                  SELECT n.seq FROM (
                    SELECT DISTINCT net_id FROM {alias}.cut_sinks
                  ) c JOIN net_records n ON n.id=c.net_id
                  LEFT JOIN endpoint_records e
                    ON e.net_seq=n.seq AND e.role=0
                  GROUP BY n.seq HAVING COUNT(e.ordinal)!=1
                )"""
            ).fetchone()[0]
        )
        if unknown:
            raise ValidationError(
                "cut-segment qualification net is absent from EmuIR"
            )
        if invalid_drivers:
            raise ValidationError(
                "cut-segment qualification net must have one driver"
            )
    finally:
        ir.connection.execute(f"DETACH DATABASE {alias}")

    cut_set = set(cut_nets)
    associated_nets = set()
    association_count = 0
    for path in path_records:
        if not isinstance(path, dict) or not isinstance(
            path.get("path_nets"), list
        ):
            raise ValidationError(
                "cut-segment qualification original path record is invalid"
            )
        path_id = path.get("id")
        if not isinstance(path_id, str) or not path_id:
            raise ValidationError(
                "cut-segment qualification original path ID is invalid"
            )
        matches = set(path["path_nets"]) & cut_set
        association_count += len(matches)
        associated_nets.update(matches)

    relation_counts = phase3_exact_relation_counts(assignment_index_path)

    timed = sum(item["status"] == "timed" for item in structural.values())
    summary = {
        "cut_nets": len(cut_nets),
        "timed_structural_nets": timed,
        "no_timed_endpoint_nets": len(cut_nets) - timed,
        "enumerated_member_associations": association_count,
        "functional_only_nets": len(cut_set - associated_nets),
    }
    return {
        "schema": CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA,
        "status": "pass",
        "provider": CUT_SEGMENT_QUALIFICATION_PROVIDER,
        "qualification": "pre-route-structural-contract-bound",
        "delay_evidence": "contract-budget-provisional",
        "routed_deadline_gate": (
            "required-phase7-endpoint-exact-or-conservative-bound"
        ),
        "storage": "indexed-summary",
        "semantic_contract_sha256": assignment.get(
            "semantic_contract_sha256"
        ),
        "cut_net_identity": _sealed_rows(cut_nets),
        "structural_identity": _sealed_rows(
            (net, structural[net]) for net in cut_nets
        ),
        "relation_counts": relation_counts,
        "summary": summary,
    }


def build_cut_segment_qualification_value(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    database: Mapping[str, Any],
    *,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
    path_records: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build qualification from one validated in-memory input set."""

    cut_nets = _cut_nets(assignment)
    segment_ids = _segment_ids_by_cut(assignment, cut_nets)
    if architecture_timing_db_path is not None:
        model, instance_cell_types = build_architecture_opensta_timing_model(
            ir, architecture_timing_db_path
        )
    else:
        model = load_timing_model(timing_model_path)
        instance_cell_types = None
    structural = classify_through_net_timing_endpoints(
        ir, model, cut_nets, instance_cell_types
    )
    member_ids: Dict[str, list[str]] = {net: [] for net in cut_nets}
    records_source = (
        path_records
        if path_records is not None
        else database.get("paths", [])
    )
    for path in records_source:
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
    net_by_id = (
        None
        if hasattr(ir, "connection")
        else {net["id"]: net for net in ir.value["nets"]}
    )
    records = []
    for net in cut_nets:
        if net_by_id is None:
            row = ir.connection.execute(
                """SELECT COUNT(*) FROM endpoint_records AS e
                     JOIN net_records AS n ON n.seq=e.net_seq
                    WHERE n.id=? AND e.role=0""",
                (net,),
            ).fetchone()
            driver_count = int(row[0])
        else:
            if net not in net_by_id:
                raise ValidationError(
                    f"cut-segment qualification net is absent from EmuIR: {net!r}"
                )
            drivers = net_by_id[net].get("drivers")
            driver_count = len(drivers) if isinstance(drivers, list) else -1
        if driver_count != 1:
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
