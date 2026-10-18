"""Explicit debugging continuation from a completed native mapped boundary.

This is not a checkpoint/cache manager or whole-design qualification command.
All placement, geometry, routing and timing work uses the normal backend APIs.
"""
from __future__ import annotations

import argparse
import gc
from pathlib import Path
import time
from typing import Any, Mapping

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .physical_backend import validate_physical_partition_result
from .platform import Platform
from .runtime import build_virtual_runtime
from .xilinx_netlist import open_xilinx_mapped_index
from .xilinx_openparf_atomic import run_xilinx_openparf_atomic_qualification
from .xilinx_openparf_bridge import materialize_xilinx_openparf_atomic_contract
from .xilinx_physical_backend import (
    _compact_openparf_qualification, _run_rapidwright_routed_backend_tail,
)
from .xilinx_rwroute import _rwroute_heap_argument


_PATH_FIELDS = (
    "completed_partition", "split_manifest", "schedule", "platform",
    "architecture", "native_constraints", "provider_manifest", "openparf_install",
    "openparf_python", "rapidwright_jar", "java", "device_data_root",
    "timing_data_dir", "opensta", "output_dir",
)


def run_native_partition_continuation(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Re-place and physically finish exactly one explicitly selected partition.

    The caller establishes producer termination before supplying the completed
    input directory. A new output directory is mandatory; this command never
    overwrites or automatically restarts an existing attempt.
    """
    paths = {}
    for field in _PATH_FIELDS:
        value = spec.get(field)
        if not isinstance(value, str) or not value:
            raise ValidationError(f"native continuation requires {field}")
        paths[field] = Path(value)
    fpga = spec.get("fpga")
    if not isinstance(fpga, str) or not fpga:
        raise ValidationError("native continuation requires fpga")
    heap = spec.get("java_heap_gib", 32)
    _rwroute_heap_argument(heap)
    for field, path in paths.items():
        if field != "output_dir" and not path.exists():
            raise ValidationError(f"native continuation {field} is missing: {path}")
    old = paths["completed_partition"]
    original = old / "rapidwright"
    mapped_path = original / "partition.mapped.sqlite"
    source_path = original / "openparf-atomic-source.json"
    region_path = original / "placement-region.json"
    java_source = Path(__file__).resolve().parents[2] / "scripts/rapidwright/EmuFlowRWRoute.java"
    for path in (mapped_path, source_path, region_path, java_source,
                 original / "mapped-netlist-report.json", old / "placement-ir-report.json",
                 old / "placement.emuir.sqlite", old / "logic-segment-identity.json",
                 old / "local-path-identity.json"):
        if not path.is_file():
            raise ValidationError(f"native continuation boundary is incomplete: {path}")
    output = paths["output_dir"]
    # Atomic directory creation is the single-attempt ownership boundary.
    # A disconnected observer cannot cause this same output to run twice.
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    stage = "completed-boundary-preflight"
    durations = {}

    def begin(name: str) -> float:
        nonlocal stage
        stage = name
        print("stage=" + stage, flush=True)
        return time.monotonic()

    try:
        manifest = read_json(paths["split_manifest"])
        clocks = manifest.get("clock_periods")
        if not isinstance(clocks, dict) or not clocks:
            raise ValidationError("completed split lacks its DUT clock-period contract")
        platform = Platform.load(paths["platform"])
        nodes = [node for node in platform.fpgas if node.id == fpga]
        if len(nodes) != 1:
            raise ValidationError("native continuation FPGA is not unique in BoardDB")
        runtime = build_virtual_runtime(read_json(paths["schedule"]), platform, clocks)
        lower = read_json(old / "placement-ir-report.json")
        mapped_report = read_json(original / "mapped-netlist-report.json")
        source = read_json(source_path)
        if (lower.get("status") != "pass" or mapped_report.get("status") != "pass"
                or source.get("status") != "pass"
                or source.get("source", {}).get("mapped_sha256") != mapped_report.get("output_sha256")):
            raise ValidationError("completed native boundary status/source identities differ")
        region = read_json(region_path)
        selected = region["global"]["allowed_slrs"]
        architecture = ArchitectureDB.load(paths["architecture"])
        part = architecture.part
        if nodes[0].part != part:
            raise ValidationError("completed platform and native device part differ")
        with open_xilinx_mapped_index(mapped_path) as mapped:
            tick = begin("native-placement-from-completed-mapped-input")
            native_root = output / "openparf-native"
            qualification = run_xilinx_openparf_atomic_qualification(
                mapped_path, source_path, paths["architecture"], native_root,
                top=mapped_report["top"], openparf_install=paths["openparf_install"],
                openparf_python=paths["openparf_python"],
                native_constraints_path=paths["native_constraints"],
                provider_manifest_path=paths["provider_manifest"],
                mapped_value=mapped, packed_value=source, architecture=architecture,
                allowed_slrs=selected,
            )
            durations["native_placement_seconds"] = time.monotonic() - tick
            certificate_path = native_root / "placement-certificate.json"
            compact = _compact_openparf_qualification(qualification, certificate_path)
            del qualification
            tick = begin("standard-native-geometry-and-bridge")
            values = {}
            packed_path, placement_path = output / "packed-sites.json", output / "placement.json"
            bridge = materialize_xilinx_openparf_atomic_contract(
                mapped_path, paths["architecture"], certificate_path, packed_path, placement_path,
                in_process_artifacts=values, top=mapped_report["top"],
                source_packed_path=source_path, constraints_path=region_path,
                native_constraints_path=paths["native_constraints"],
                provider_manifest_path=paths["provider_manifest"],
                mapped_value=mapped, source_packed_value=source,
                mapped_sha256=mapped_report["output_sha256"], architecture=architecture,
            )
            packed, placement = values.pop("packed"), values.pop("placement")
            packing_check, placement_check = values.pop("packing_validation"), values.pop("placement_validation")
            source_summary = source["summary"]
            del source, architecture
            gc.collect()
            durations["native_bridge_seconds"] = time.monotonic() - tick
            tick = begin("normal-rwroute-and-physical-timing")
            implementation = _run_rapidwright_routed_backend_tail(
                fpga=fpga, part=part, merged_ir_path=old / "placement.emuir.sqlite",
                mapped_path=mapped_path, mapped_report=mapped_report,
                packed_path=packed_path, placement_path=placement_path,
                mapped_value=mapped, packed_value=packed, placement_value=placement,
                runtime=runtime, original_cells=lower["original_instances"],
                transport_cells=lower["transport_instances"], output_dir=output,
                boundary_identity_path=Path(lower["boundary_identity"]["output"]),
                rapidwright_jar=paths["rapidwright_jar"], java=paths["java"],
                classes_dir=output / "classes", java_source=java_source,
                device_data_root=paths["device_data_root"], timing_data_dir=paths["timing_data_dir"],
                opensta=str(paths["opensta"]), logic_identity_path=old / "logic-segment-identity.json",
                local_identity_path=old / "local-path-identity.json",
                provider="rapidwright-rwroute-openparf-native-candidate-v1",
                packing_stage={"source": source_summary, "result": packed["summary"], "validation": packing_check},
                placement_stage={"region": region["global"], "native_openparf": compact,
                                 "bridge": bridge, "result": placement["summary"], "validation": placement_check},
                placement_artifacts={"placement_region": region_path, "openparf_atomic_source": source_path,
                                     "openparf_atomic_certificate": certificate_path},
                mapped_sha256=mapped_report["output_sha256"], java_heap_gib=heap,
            )
            durations["routing_and_physical_timing_seconds"] = time.monotonic() - tick
        validate_physical_partition_result(
            implementation["result"], backend="rapidwright", fpga=fpga, part=part,
            original_cells=lower["original_instances"], transport_cells=lower["transport_instances"],
        )
        record = {"fpga": fpga, "part": part, "status": "pass", "active": True,
                  "original_cells": lower["original_instances"], "transport_cells": lower["transport_instances"],
                  "critical_path_ns": implementation["result"]["timing"]["critical_path_ns"],
                  "stages": {"transport_synthesis": {"status": "pass", "execution": "completed-upstream-not-rerun",
                              "instances": lower["transport_instances"], "output": str(old / "transport.emuir.sqlite")},
                             "placement_ir": lower, "rapidwright_implementation": implementation},
                  "physical_result": implementation["result"]}
        write_json(output / "partition-record.json", record, compact=True)
        result = {"status": "pass", "fpga": fpga, "seconds": time.monotonic() - started,
                  "stage_seconds": durations, "record": str(output / "partition-record.json"),
                  "scope": "one explicit physical debugging continuation; not whole-design Phase7C",
                  "no_synthesis": True, "no_partition": True, "no_phase4_5_6": True}
        write_json(output / "result.json", result, compact=True)
        return result
    except BaseException as error:
        write_json(output / "result.json", {"status": "failed", "fpga": fpga, "stage": stage,
                   "error": str(error), "seconds": time.monotonic() - started,
                   "stage_seconds": durations}, compact=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    run_native_partition_continuation(read_json(args.spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
