"""Real, fail-closed DREAMPlaceFPGA Interchange fixture runner.

This module deliberately stops before the production RapidWright bridge.  The
pinned upstream placer has global placement, packing, and legalization, but no
qualified UltraScale+ detailed-placement stage or complete macro/clock/SLR
constraint model.  A successful run is therefore useful native-engine
evidence while remaining ineligible for Route A physical implementation.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .dreamplacefpga_candidate import (
    DREAMPLACEFPGA_NATIVE_RUN_SCHEMA,
    DREAMPLACEFPGA_UPSTREAM_REVISION,
    assess_dreamplacefpga_candidate,
    run_dreamplacefpga_interchange_probe,
)
from .dreamplacefpga_interchange import (
    build_dreamplacefpga_placement_candidate,
    validate_dreamplacefpga_placement_candidate,
    write_dreamplacefpga_logical_netlist,
)
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import validate_xilinx_packing


DREAMPLACEFPGA_RAPIDWRIGHT_BOUNDARY_SCHEMA = (
    "emuflow.dreamplacefpga-rapidwright-boundary/v1"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_dreamplacefpga_rapidwright_boundary(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    physical_netlist: Path,
    placement_candidate_path: Path,
    output_path: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Seal the exact blocked handoff without manufacturing a placement.

    RapidWright's router consumes EmuFlow's standard packed and exact Xilinx
    placement contracts.  The DREAMPlaceFPGA result is intentionally kept in
    a distinct candidate schema until upstream supplies qualified detailed
    placement and the missing physical constraints.  No greedy/legalizer
    fallback is called here.
    """

    packing = validate_xilinx_packing(
        mapped_path,
        packed_path,
        top=top,
        architecture_path=architecture_path,
    )
    placement = validate_dreamplacefpga_placement_candidate(
        mapped_path,
        architecture_path,
        physical_netlist,
        placement_candidate_path,
        top=top,
        packed_path=packed_path,
    )
    candidate = read_json(placement_candidate_path)
    blockers = [
        "stages.detailed_placement:core_missing",
        "constraints.cascade_relative_placement:core_missing",
        "constraints.clock_regions:core_missing",
        "constraints.multi_slr_regions:core_missing",
    ]
    result = {
        "schema": DREAMPLACEFPGA_RAPIDWRIGHT_BOUNDARY_SCHEMA,
        "status": "blocked",
        "provider": "dreamplacefpga-interchange-candidate-v1",
        "upstream_revision": DREAMPLACEFPGA_UPSTREAM_REVISION,
        "rapidwright_eligible": False,
        "source": {
            "mapped_sha256": _sha256(mapped_path),
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
            "physical_netlist_sha256": _sha256(physical_netlist),
            "placement_candidate_sha256": _sha256(placement_candidate_path),
        },
        "validated_boundary": {
            "packing": packing["status"],
            "placement_candidate": placement["status"],
            "packed_clusters_preserved": candidate["summary"][
                "packed_clusters_preserved"
            ],
            "exact_packed_bels": candidate["summary"][
                "exact_packed_bels"
            ],
        },
        "required_downstream_contracts": {
            "packed": "emuflow.packed-site-netlist/v1",
            "placement": "emuflow.xilinx-placement/v1",
            "route": "emuflow.xilinx-route-db/v1",
        },
        "blockers": blockers,
        "fallback_used": False,
    }
    write_json(output_path, result, compact=True)
    return result


def run_dreamplacefpga_native_fixture(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    interchange_device: Path,
    output_dir: Path,
    *,
    dreamplace_root: Path,
    python: Path = Path(sys.executable),
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the pinned upstream placer and independently certify its output."""

    if output_dir.exists():
        raise ValidationError(
            "DREAMPlaceFPGA native output directory already exists"
        )
    output_dir.mkdir(parents=True)
    packing = validate_xilinx_packing(
        mapped_path,
        packed_path,
        top=top,
        architecture_path=architecture_path,
    )
    logical_netlist = output_dir / "design.netlist"
    logical_export = write_dreamplacefpga_logical_netlist(
        mapped_path, logical_netlist, top=top
    )
    capability_path = output_dir / "capability.json"
    capability = assess_dreamplacefpga_candidate(
        mapped_path,
        top=top,
        dreamplace_root=dreamplace_root,
        python=python,
        interchange_device=interchange_device,
        interchange_netlist=logical_netlist,
        report_path=capability_path,
    )
    probe = run_dreamplacefpga_interchange_probe(
        capability, output_dir / "upstream", python=python
    )
    physical_netlist = Path(probe["physical_netlist"])
    placement_candidate = output_dir / "placement-candidate.json"
    placement = build_dreamplacefpga_placement_candidate(
        mapped_path,
        architecture_path,
        physical_netlist,
        placement_candidate,
        top=top,
        packed_path=packed_path,
    )
    boundary_path = output_dir / "rapidwright-boundary.json"
    boundary = build_dreamplacefpga_rapidwright_boundary(
        mapped_path,
        packed_path,
        architecture_path,
        physical_netlist,
        placement_candidate,
        boundary_path,
        top=top,
    )
    result = {
        "schema": DREAMPLACEFPGA_NATIVE_RUN_SCHEMA,
        "status": "pass",
        "provider": "dreamplacefpga-interchange-candidate-v1",
        "runtime_validation": "native-upstream-process",
        "upstream_revision": DREAMPLACEFPGA_UPSTREAM_REVISION,
        "stages": {
            "packing_input": packing["status"],
            "logical_export": logical_export["status"],
            "upstream_placement": probe["status"],
            "placement_import": placement["status"],
            "rapidwright_boundary": boundary["status"],
        },
        "artifacts": {
            "capability": {
                "path": str(capability_path), "sha256": _sha256(capability_path),
            },
            "logical_netlist": {
                "path": str(logical_netlist), "sha256": _sha256(logical_netlist),
            },
            "physical_netlist": {
                "path": str(physical_netlist), "sha256": _sha256(physical_netlist),
            },
            "placement_candidate": {
                "path": str(placement_candidate),
                "sha256": _sha256(placement_candidate),
            },
            "rapidwright_boundary": {
                "path": str(boundary_path), "sha256": _sha256(boundary_path),
            },
        },
        "production_qualified": False,
        "rapidwright_eligible": False,
        "blockers": boundary["blockers"],
        "fallback_used": False,
    }
    write_json(output_dir / "native-run.json", result, compact=True)
    return result
