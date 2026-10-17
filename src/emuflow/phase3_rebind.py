"""Explicit debugging continuation with frozen instance ownership.

This is not a partition provider, cache, or production fallback. After a user
requests continuation from a repaired circuit, rebuild its structural contract
without calling an optimizer or inheriting stale cut/segment records.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .combinational_cut import STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
from .errors import ValidationError
from .io import read_json, write_json
from .partition import (
    CUT_MODE_SEQUENTIAL_ONLY,
    build_clusters,
    build_partition_assignment,
    load_partition_constraints,
    validate_partition_artifacts_online,
)
from .partition_hops import validate_assignment_hops
from .partition_ir import load_partition_ir
from .phase3 import PHASE3_REPORT_SCHEMA
from .phase3_assignment_index import open_phase3_assignment_index, write_phase3_assignment_index
from .phase3_storage import compress_phase3_clusters, pack_phase3_assignment
from .platform import Platform
from .routing import load_route_constraints


def rebind_phase3_assignment(
    ir_path: Path,
    platform_path: Path,
    frozen_phase3: Path,
    output_dir: Path,
    *,
    route_constraints_path: Path | None = None,
    max_cross_fpga_dependency_depth: int = STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
) -> dict[str, Any]:
    """Rebuild cuts and dependencies, preserving every primary instance owner.

    Current clustering proves fixed, CDC/SCC/cascade, and cut-policy legality;
    a frozen assignment that splits a current atomic cluster is rejected.
    Resource/balance limits are taken explicitly from the frozen experiment,
    not relaxed or replaced with today's invocation defaults. Only partition
    identities/resources/connectivity enter Python, never full circuit payloads.
    """
    if output_dir.exists():
        raise ValidationError("assignment rebind requires a new output directory")
    old = read_json(frozen_phase3 / "phase3_report.json")
    platform = Platform.load(platform_path)
    ir = load_partition_ir(ir_path)
    design = ir.value["design"]["name"]
    if (
        old.get("schema") != PHASE3_REPORT_SCHEMA
        or old.get("status") != "pass"
        or old.get("design") != design
        or old.get("platform") != platform.name
    ):
        raise ValidationError("frozen Phase 3 report identity/status disagrees")
    connection = open_phase3_assignment_index(frozen_phase3 / "assignment.sqlite")
    try:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if metadata.get("design") != design or metadata.get("platform") != platform.name:
            raise ValidationError("frozen assignment index identity disagrees")
        if connection.execute("SELECT 1 FROM copies WHERE is_replica=1 LIMIT 1").fetchone():
            raise ValidationError("assignment rebind does not support replicated instances")
        # Stream only the compact ownership relation, not the compatibility JSON
        # loader (which would expand the old cluster graph and old semantics).
        owners = {
            row["copy_id"]: row["fpga"]
            for row in connection.execute(
                "SELECT c.copy_id,p.fpga FROM copies c "
                "JOIN parts p ON p.part_index=c.part_index WHERE c.is_replica=0"
            )
        }
    finally:
        connection.close()
    source_count = len(ir.value["instances"])
    if len(owners) != source_count or metadata.get("source_instance_count") != source_count:
        raise ValidationError("frozen assignment instance coverage disagrees")
    constraints = load_partition_constraints(
        frozen_phase3 / "constraints.normalized.json", ir, platform
    )
    cut_mode = old.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
    options = {"cut_mode": cut_mode,
               "max_cross_fpga_dependency_depth": max_cross_fpga_dependency_depth}
    if old.get("static_exact_candidate_policy") is not None:
        options["static_exact_candidate_policy"] = old["static_exact_candidate_policy"]
    clusters = build_clusters(ir, constraints, **options)
    cluster_assignment = {}
    covered = 0
    for cluster in clusters["clusters"]:
        try:
            targets = {owners[member] for member in cluster["instances"]}
        except KeyError as error:
            raise ValidationError("frozen assignment instance identity disagrees") from error
        if len(targets) != 1:
            raise ValidationError("frozen assignment splits a current atomic cluster")
        cluster_assignment[cluster["id"]] = next(iter(targets))
        covered += len(cluster["instances"])
    if covered != source_count:
        raise ValidationError("current clusters do not cover frozen instance ownership")
    # Cluster construction guarantees disjoint exact coverage. Each member was
    # matched above; no second whole-graph identity/set reconstruction is needed.
    del owners
    assignment = build_partition_assignment(
        ir, platform, clusters, constraints, cluster_assignment,
        provider=str(old["provider"]), seed=int(old["seed"]),
        provider_metadata={"execution": "frozen-instance-ownership-rebind-v1"},
    )
    validation = validate_partition_artifacts_online(platform, clusters, assignment)
    hops = validate_assignment_hops(
        platform, assignment, load_route_constraints(route_constraints_path, platform)
    )
    report = {
        "schema": PHASE3_REPORT_SCHEMA, "phase": 3, "status": "pass",
        "design": design, "platform": platform.name,
        "provider": assignment["provider"], "seed": assignment["seed"],
        "requested_provider": "frozen-instance-ownership-rebind-v1",
        "validation": validation, "hop_refinement": hops,
        "cut_mode": cut_mode,
        "static_exact_candidate_policy": old.get("static_exact_candidate_policy"),
        "partitions": [{k: v for k, v in p.items() if k != "clusters"}
                       for p in assignment["partitions"]],
        "partition_decision": {"executed": False,
                               "reason": "explicit-debugging-continuation",
                               "preserved_instance_owners": covered},
        "artifacts": {"clusters": "clusters.json",
                      "constraints": "constraints.normalized.json",
                      "assignment": "assignment.json",
                      "assignment_index": "assignment.sqlite",
                      "report": "phase3_report.json"},
    }
    output_dir.mkdir(parents=True)
    write_json(output_dir / "clusters.json", compress_phase3_clusters(clusters), compact=True)
    write_json(output_dir / "constraints.normalized.json", constraints, compact=True)
    write_json(output_dir / "assignment.json", pack_phase3_assignment(assignment, clusters), compact=True)
    write_phase3_assignment_index(output_dir / "assignment.sqlite", ir, assignment)
    # Report is the publication boundary; a partial directory is not complete.
    write_json(output_dir / "phase3_report.json", report, compact=True)
    return report
