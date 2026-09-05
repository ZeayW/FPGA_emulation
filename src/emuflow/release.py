from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .benchmark import BENCHMARK_REPORT_SCHEMA
from .errors import ValidationError
from .io import read_json, write_json
from .lowering import PLACEMENT_IR_REPORT_SCHEMA
from .phase2 import PHASE2_REPORT_SCHEMA
from .phase3 import PHASE3_REPORT_SCHEMA
from .phase4 import PHASE4_REPORT_SCHEMA
from .phase5 import PHASE5_REPORT_SCHEMA
from .phase6 import PHASE6_REPORT_SCHEMA
from .phase7c import PHASE7C_REPORT_SCHEMA, system_timing_summary
from .platform import Platform
from .runtime import (
    PHYSICAL_SUMMARY_SCHEMA,
    QOR_REPORT_SCHEMA,
    VIRTUAL_RUNTIME_SCHEMA,
    validate_physical_summary,
)
from .system_timing import SYSTEM_TIMING_SCHEMA
from .verilog import MAPPED_VERILOG_REPORT_SCHEMA


RELEASE_MANIFEST_SCHEMA = "emuflow.release-manifest/v1"
PHASE7D_REPORT_SCHEMA = "emuflow.phase7d-report/v1"
LEGACY_PHASE7C_REPORT_SCHEMA = "emuflow.phase7c-report/v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_report(
    report: Mapping[str, Any],
    schema: str,
    name: str,
    design: str,
    platform: str,
) -> None:
    if report.get("schema") != schema:
        raise ValidationError(f"{name} has the wrong schema")
    if report.get("status") != "pass":
        raise ValidationError(f"{name} did not pass")
    if report.get("design") != design:
        raise ValidationError(f"{name} design does not match release")
    if report.get("platform") != platform:
        raise ValidationError(f"{name} platform does not match release")


def _validate_sources(benchmark: Mapping[str, Any]) -> Sequence[Dict[str, Any]]:
    source = benchmark.get("source")
    if not isinstance(source, dict):
        raise ValidationError("benchmark source record is missing")
    root = Path(str(source.get("root", ""))).resolve()
    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("benchmark source file inventory is empty")
    records = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValidationError(
                f"benchmark source file {index} is not an object"
            )
        relative = item.get("path")
        expected = item.get("sha256")
        if not isinstance(relative, str) or not relative:
            raise ValidationError(
                f"benchmark source file {index} has no path"
            )
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValidationError(
                f"benchmark source file escapes or is missing: {relative}"
            )
        actual = _sha256(path)
        if actual != expected:
            raise ValidationError(
                f"benchmark source hash mismatch: {relative}"
            )
        records.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": actual}
        )
    return sorted(records, key=lambda item: item["path"])


def _artifact_inventory(
    artifact_paths: Mapping[str, Path],
) -> Sequence[Dict[str, Any]]:
    if not artifact_paths:
        raise ValidationError("release artifact inventory cannot be empty")
    records = []
    for label, path in sorted(artifact_paths.items()):
        if not label:
            raise ValidationError("release artifact label cannot be empty")
        if not path.is_file():
            raise ValidationError(f"release artifact is missing: {label}")
        records.append(
            {
                "label": label,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def build_release_manifest(
    benchmark: Mapping[str, Any],
    phase3: Mapping[str, Any],
    phase4: Mapping[str, Any],
    phase5: Mapping[str, Any],
    phase6: Mapping[str, Any],
    phase7c: Mapping[str, Any],
    runtime: Mapping[str, Any],
    qor: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
    lowering_reports: Mapping[str, Mapping[str, Any]],
    placement_reports: Mapping[str, Mapping[str, Any]],
    emission_reports: Mapping[str, Mapping[str, Any]],
    artifact_paths: Mapping[str, Path],
    source_commit: str,
) -> Dict[str, Any]:
    if benchmark.get("schema") != BENCHMARK_REPORT_SCHEMA:
        raise ValidationError("benchmark report has the wrong schema")
    if benchmark.get("status") != "pass":
        raise ValidationError("benchmark report did not pass")
    design = benchmark.get("design_id")
    if not isinstance(design, str) or not design:
        raise ValidationError("benchmark report has no design_id")
    if not isinstance(source_commit, str) or not source_commit:
        raise ValidationError("release source commit must be non-empty")
    gates = benchmark.get("gates")
    if not isinstance(gates, dict) or any(
        gates.get(name) != "pass"
        for name in (
            "G0_source",
            "G1_elaboration",
            "G2_synthesis",
            "G3_emuir",
        )
    ):
        raise ValidationError("benchmark G0-G3 gates did not all pass")
    if benchmark.get("phase1", {}).get("design") != design:
        raise ValidationError("benchmark Phase 1 design is inconsistent")
    if benchmark.get("phase1", {}).get("platform") != platform.name:
        raise ValidationError("benchmark Phase 1 platform is inconsistent")

    _require_report(
        phase3, PHASE3_REPORT_SCHEMA, "Phase 3", design, platform.name
    )
    _require_report(
        phase4, PHASE4_REPORT_SCHEMA, "Phase 4", design, platform.name
    )
    _require_report(
        phase5, PHASE5_REPORT_SCHEMA, "Phase 5", design, platform.name
    )
    _require_report(
        phase6, PHASE6_REPORT_SCHEMA, "Phase 6", design, platform.name
    )
    phase7c_schema = phase7c.get("schema")
    if phase7c_schema not in {
        LEGACY_PHASE7C_REPORT_SCHEMA,
        PHASE7C_REPORT_SCHEMA,
    }:
        raise ValidationError("Phase 7C has the wrong schema")
    if (
        phase7c.get("status") != "pass"
        or phase7c.get("design") != design
        or phase7c.get("platform") != platform.name
    ):
        raise ValidationError("Phase 7C did not pass or has the wrong identity")
    if runtime.get("schema") != VIRTUAL_RUNTIME_SCHEMA:
        raise ValidationError("runtime contract has the wrong schema")
    if runtime.get("design") != design or runtime.get("platform") != platform.name:
        raise ValidationError("runtime contract does not match release")
    if qor.get("schema") != QOR_REPORT_SCHEMA or qor.get("status") != "pass":
        raise ValidationError("QoR report did not pass")
    if physical_summary.get("schema") != PHYSICAL_SUMMARY_SCHEMA:
        raise ValidationError("physical summary has the wrong schema")
    physical = validate_physical_summary(
        physical_summary, runtime, platform
    )
    system_timing = qor.get("timing")
    if (
        not isinstance(system_timing, dict)
        or system_timing.get("schema") != SYSTEM_TIMING_SCHEMA
        or system_timing.get("status") != "pass"
    ):
        raise ValidationError("Phase 7C unified system timing did not pass")
    phase7c_timing_matches = (
        phase7c.get("system_timing") == system_timing
        if phase7c_schema == LEGACY_PHASE7C_REPORT_SCHEMA
        else (
            phase7c.get("system_timing_ref")
            == {"artifact": "qor_report", "json_pointer": "/timing"}
            and phase7c.get("system_timing_summary")
            == system_timing_summary(system_timing)
        )
    )
    if not phase7c_timing_matches:
        raise ValidationError("Phase 7C/QoR system timing reports disagree")

    p3 = phase3["validation"]
    p4 = phase4["validation"]
    p5 = phase5["validation"]
    p6 = phase6["validation"]
    eq = phase6["equivalence"]
    if p3.get("illegal_cuts") != 0:
        raise ValidationError("G4 contains illegal cuts")
    if p4.get("overloaded_links") != 0:
        raise ValidationError("G5 contains overloaded links")
    if p5.get("collisions") != 0:
        raise ValidationError("G6 contains TDM collisions")
    if eq.get("mismatches") != 0 or eq.get("cycles", 0) <= 0:
        raise ValidationError("G6 mapped cycle equivalence did not pass")
    if (
        p6.get("instance_coverage_errors") != 0
        or p6.get("endpoint_agreement_errors") != 0
        or p6.get("unbound_package_pins") != p6.get("virtual_anchors")
    ):
        raise ValidationError("G7 logical lane/anchor checks are inconsistent")

    cut_nets = p3.get("cut_nets")
    if not (
        cut_nets
        == p4.get("demands")
        == p5.get("demands")
    ):
        raise ValidationError(
            "cut and routed-demand counts do not agree"
        )
    if p3.get("cut_sink_endpoints") != p6.get("cut_sink_endpoints"):
        raise ValidationError(
            "partitioned and split logical sink endpoint counts do not agree"
        )
    if p4.get("routed_sinks") != p5.get("routed_sinks"):
        raise ValidationError(
            "routed and scheduled remote sink counts do not agree"
        )
    if not (
        p4.get("total_link_bit_hops")
        == p5.get("scheduled_bit_hops")
        == p6.get("scheduled_hops")
        == p6.get("lane_map_entries")
    ):
        raise ValidationError(
            "routed, scheduled, split, and bound bit-hop counts do not agree"
        )
    original_cells = p3.get("instances")
    if not (
        original_cells
        == p6.get("instances")
        == physical.get("original_cells")
    ):
        raise ValidationError("original cell counts do not agree")
    if runtime["frame"]["completion_slot"] != p5.get("completion_slot"):
        raise ValidationError("runtime completion slot does not match TDM")
    if runtime["frame"]["slots"] != p5.get("frame_slots"):
        raise ValidationError("runtime frame length does not match TDM")

    fpga_ids = {fpga.id for fpga in platform.fpgas}
    for name, reports in (
        ("lowering", lowering_reports),
        ("placement", placement_reports),
        ("emission", emission_reports),
    ):
        if set(reports) != fpga_ids:
            raise ValidationError(
                f"{name} reports must cover every FPGA exactly once"
            )
    physical_by_fpga = {
        item["fpga"]: item for item in physical_summary["fpgas"]
    }
    placement_cells = 0
    transport_cells = 0
    for fpga_id in sorted(fpga_ids):
        lowering = lowering_reports[fpga_id]
        placement = placement_reports[fpga_id]
        emission = emission_reports[fpga_id]
        if (
            lowering.get("schema") != PLACEMENT_IR_REPORT_SCHEMA
            or lowering.get("status") != "pass"
        ):
            raise ValidationError(f"{fpga_id} lowering report did not pass")
        if (
            placement.get("schema") != PHASE2_REPORT_SCHEMA
            or placement.get("status") != "pass"
            or placement.get("provider") != "openparf-root-build"
            or placement.get("placement", {}).get("status") != "legal"
        ):
            raise ValidationError(
                f"{fpga_id} root-built OpenPARF placement report did not pass"
            )
        if (
            emission.get("schema") != MAPPED_VERILOG_REPORT_SCHEMA
            or emission.get("status") != "pass"
        ):
            raise ValidationError(f"{fpga_id} emission report did not pass")
        merged = lowering.get("instances")
        if not (
            merged
            == placement.get("placement", {}).get("cells")
            == emission.get("instances")
            == physical_by_fpga[fpga_id].get("routed_cells")
        ):
            raise ValidationError(
                f"{fpga_id} merged/placed/emitted/routed cells disagree"
            )
        placement_cells += merged
        transport_cells += lowering.get("transport_instances", 0)
    if placement_cells != physical["routed_cells"]:
        raise ValidationError("G8/G9 total routed cell count mismatch")
    if transport_cells != physical["transport_cells"]:
        raise ValidationError("transport overhead count mismatch")
    if phase7c.get("physical") != physical or qor.get("physical") != physical:
        raise ValidationError("Phase 7C/QoR physical summaries disagree")

    source_records = _validate_sources(benchmark)
    artifacts = _artifact_inventory(artifact_paths)
    gate_records = {
        "G0": {
            "status": "pass",
            "evidence": f"{len(source_records)} source files rehashed",
        },
        "G1": {"status": "pass", "evidence": "elaboration report passed"},
        "G2": {"status": "pass", "evidence": "mapped synthesis passed"},
        "G3": {
            "status": "pass",
            "evidence": f"{original_cells} EmuIR instances",
        },
        "G4": {
            "status": "pass",
            "evidence": f"{cut_nets} legal cuts across {p3['used_fpgas']} FPGAs",
        },
        "G5": {
            "status": "pass",
            "evidence": f"{p4['routed_sinks']} routed sinks, zero overload",
        },
        "G6": {
            "status": "pass",
            "evidence": (
                f"{p5['scheduled_bit_hops']} hops, "
                f"{eq['cycles']} equivalent cycles"
            ),
        },
        "G7": {
            "status": "pass",
            "evidence": (
                f"{p6['lane_map_entries']} logical lanes, "
                f"{p6['virtual_anchors']} virtual anchors"
            ),
        },
        "G8": {
            "status": "pass",
            "evidence": (
                f"{placement_cells} legal OpenPARF-placed cells"
            ),
        },
        "G9": {
            "status": "pass",
            "evidence": (
                f"{physical['routed_cells']} routed cells, "
                f"WNS {physical['worst_wns_ns']} ns"
            ),
        },
    }
    return {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "status": "pass",
        "release_scope": "board-independent-g0-g9",
        "source_commit": source_commit,
        "benchmark": benchmark["benchmark"],
        "design": design,
        "platform": platform.name,
        "semantic_envelope": runtime["semantic_envelope"],
        "gates": gate_records,
        "metrics": {
            "source_files": len(source_records),
            "original_cells": original_cells,
            "transport_cells": physical["transport_cells"],
            "routed_cells": physical["routed_cells"],
            "physical_cells": physical["physical_cells"],
            "infrastructure_cells": physical["infrastructure_cells"],
            "cut_nets": cut_nets,
            "scheduled_bit_hops": p5["scheduled_bit_hops"],
            "equivalence_cycles": eq["cycles"],
            "nominal_virtual_frequency_mhz": runtime[
                "virtual_dut_clock"
            ]["nominal_frequency_mhz"],
            "worst_wns_ns": physical["worst_wns_ns"],
        },
        "sources": source_records,
        "artifacts": artifacts,
        "board_binding": runtime["board_binding"],
    }


def run_phase7d(
    benchmark_report_path: Path,
    phase3_report_path: Path,
    phase4_report_path: Path,
    phase5_report_path: Path,
    phase6_report_path: Path,
    phase7c_report_path: Path,
    runtime_contract_path: Path,
    qor_report_path: Path,
    physical_summary_path: Path,
    platform_path: Path,
    lowering_report_paths: Mapping[str, Path],
    placement_report_paths: Mapping[str, Path],
    emission_report_paths: Mapping[str, Path],
    artifact_paths: Mapping[str, Path],
    source_commit: str,
    output_dir: Path,
) -> Dict[str, Any]:
    manifest = build_release_manifest(
        read_json(benchmark_report_path),
        read_json(phase3_report_path),
        read_json(phase4_report_path),
        read_json(phase5_report_path),
        read_json(phase6_report_path),
        read_json(phase7c_report_path),
        read_json(runtime_contract_path),
        read_json(qor_report_path),
        read_json(physical_summary_path),
        Platform.load(platform_path),
        {
            fpga: read_json(path)
            for fpga, path in lowering_report_paths.items()
        },
        {
            fpga: read_json(path)
            for fpga, path in placement_report_paths.items()
        },
        {
            fpga: read_json(path)
            for fpga, path in emission_report_paths.items()
        },
        artifact_paths,
        source_commit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "release_manifest.json", manifest)
    report = {
        "schema": PHASE7D_REPORT_SCHEMA,
        "phase": "7D",
        "increment": "board-independent-release-audit",
        "status": "pass",
        "design": manifest["design"],
        "platform": manifest["platform"],
        "gates": manifest["gates"],
        "metrics": manifest["metrics"],
        "artifacts": {
            "release_manifest": "release_manifest.json",
            "report": "phase7d_report.json",
        },
    }
    write_json(output_dir / "phase7d_report.json", report)
    return report
