"""Read-only, bounded binding of completed indexed frontend/timing boundaries."""

from pathlib import Path
from typing import Mapping

from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform
from .split_netlist_storage import read_compact_emuir_control_metadata


def bind_prepared_inputs(frontend, timing, phase1_out, timing_out, platform_path,
                         clock_periods: Mapping[str, float], mapping_profile):
    frontend, timing = Path(frontend).resolve(), Path(timing).resolve()
    phase1 = frontend / "phase1" if (frontend / "phase1").is_dir() else frontend
    source_ir = phase1 / "design.emuir.sqlite"
    report_path = phase1 / "phase1_report.json"
    if not report_path.is_file():
        report_path = phase1 / "phase1_report.source.json"
    report = read_json(report_path)
    control = read_compact_emuir_control_metadata(source_ir)
    audit = report.get("indexed_staging_transform", {}).get("primitive_audit", {})
    if report.get("status") != "pass" or audit.get("status") != "pass":
        raise ValidationError("prepared Phase 1 lacks a passing publication/mapping report")
    if audit.get("mapping_profile") != mapping_profile:
        raise ValidationError("prepared Phase 1 mapping profile mismatch")
    if report.get("design") != control["design"]["name"]:
        raise ValidationError("prepared Phase 1 design mismatch")
    if {c["id"] for c in control["clocks"]} != set(clock_periods or {}):
        raise ValidationError("prepared continuation must preserve every DUT clock")
    old_platform = Platform.load(phase1 / "platform.normalized.json")
    platform = Platform.load(platform_path)
    if {f.part for f in old_platform.fpgas} != {f.part for f in platform.fpgas}:
        raise ValidationError("prepared Phase 1 device part differs from the selected platform")
    timing_report = read_json(timing / "experiment-timing-report.json")
    if timing_report.get("status") != "pass" or timing_report.get("clocks") != dict(clock_periods):
        raise ValidationError("prepared Phase 2 publication or clock constraints mismatch")
    database = read_json(timing / "path-database.json")
    if database.get("schema") != "emuflow.sta-path-database/v3" or database.get("design") != report["design"]:
        raise ValidationError("prepared Phase 2 must be the indexed database for this design")
    weights = read_json(timing / "partition-net-weights.json")
    phase1_out.mkdir(parents=True, exist_ok=True)
    timing_out.mkdir(parents=True, exist_ok=True)
    (phase1_out / "design.emuir.sqlite").symlink_to(source_ir)
    for name, value in (("path-database.json", database), ("partition-net-weights.json", weights)):
        # Payload names must remain local to the bound manifest. Reuse their
        # immutable bytes via symlinks; never copy or rehash a full population.
        for payload in value.get("payloads", {}).values():
            relative = Path(payload["path"])
            if relative.is_absolute() or relative.name != str(relative):
                raise ValidationError("prepared payload must be a sibling of its manifest")
            source = timing / relative
            if not source.is_file():
                raise ValidationError("prepared indexed payload is missing")
            (timing_out / relative).symlink_to(source)
        write_json(timing_out / name, value, compact=True)
    write_json(phase1_out / "platform.normalized.json", platform.to_dict())
    bound_report = {**report, "platform": platform.name,
                    "continuation": {"mode": "read-only-indexed-boundary", "source": str(source_ir)}}
    write_json(phase1_out / "phase1_report.json", bound_report, compact=True)
    return bound_report, {
        "status": "pass", "mode": "validated-existing-phase2", "backend": "opensta",
        "sta": timing_report.get("sta", {}), "optimization_enabled": True,
        "partition_weights": timing_report.get("weights", {}),
    }
