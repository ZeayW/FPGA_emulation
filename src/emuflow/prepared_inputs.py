"""Read-only, bounded binding of completed indexed frontend/timing boundaries."""

from pathlib import Path
import sqlite3
from typing import Mapping

from .errors import ValidationError
from .io import read_json, write_json
from .platform import BOARDDB_SCHEMA, Platform
from .split_netlist_storage import read_compact_emuir_control_metadata


def _prepared_device_parts(platform_path: Path):
    """Read the device identity from a historical Phase-1 board snapshot.

    A prepared boundary only contributes its indexed design database.  The
    selected platform is rebound below and remains the authoritative topology,
    link-delay, and TDM-ratio contract.  Reading only the immutable device-part
    identity keeps older boundaries usable when BoardDB gains new required link
    fields, without assigning defaults to those historical link records.
    """
    document = read_json(platform_path)
    if document.get("schema") != "emuflow.boarddb/v1":
        raise ValidationError("prepared Phase 1 platform schema is unsupported")
    fpgas = document.get("fpgas")
    if not isinstance(fpgas, list) or not fpgas:
        raise ValidationError("prepared Phase 1 platform FPGA inventory is invalid")
    parts = set()
    for fpga in fpgas:
        if not isinstance(fpga, dict):
            raise ValidationError("prepared Phase 1 platform FPGA inventory is invalid")
        part = fpga.get("part")
        if not isinstance(part, str) or not part:
            raise ValidationError("prepared Phase 1 device identity: platform device part is invalid")
        parts.add(part)
    return parts


def _published_timing_report(timing, database, weights, clock_periods):
    """Validate atomically published native flow payloads without rerunning STA."""
    if weights.get("schema") != "emuflow.partition-net-weights/v2" or weights.get("design") != database.get("design"):
        raise ValidationError("prepared indexed timing/weight identity mismatch")
    payloads = []
    for value, key, fmt in ((database, "paths", "sqlite-sta-path/v1"),
                            (weights, "weights", "sqlite-net-weights/v1")):
        descriptor = value.get("payloads", {}).get(key, {})
        name = descriptor.get("path")
        if (not isinstance(name, str) or Path(name).name != name
            or descriptor.get("format") != fmt
            or not isinstance(descriptor.get("records"), int)
            or descriptor["records"] <= 0 or not (timing / name).is_file()):
            raise ValidationError("prepared published timing payload is invalid or missing")
        payloads.append((timing / name, descriptor["records"]))
    try:
        with sqlite3.connect(f"file:{payloads[0][0]}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValidationError("prepared timing SQLite version mismatch")
            rows = list(connection.execute(
                "SELECT clock_domain,clock_period_ns,COUNT(*) FROM paths "
                "GROUP BY clock_domain,clock_period_ns"))
        observed = {name: float(period) for name, period, _count in rows}
        if len(rows) != len(observed) or observed != dict(clock_periods):
            raise ValidationError("prepared Phase 2 clock constraints mismatch")
        if sum(row[2] for row in rows) != payloads[0][1] or database.get("summary", {}).get("paths") != payloads[0][1]:
            raise ValidationError("prepared timing path publication count mismatch")
        with sqlite3.connect(f"file:{payloads[1][0]}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValidationError("prepared weight SQLite version mismatch")
            count = connection.execute("SELECT COUNT(*) FROM net_weights").fetchone()[0]
        if count != payloads[1][1] or weights.get("summary", {}).get("weighted_nets") != count:
            raise ValidationError("prepared net-weight publication count mismatch")
    except sqlite3.DatabaseError as error:
        raise ValidationError("prepared timing SQLite publication is invalid") from error
    return {"status": "pass", "clocks": dict(clock_periods),
            "sta": {"engine": database.get("source", {}).get("engine"),
                    "paths": payloads[0][1]},
            "weights": {"weighted_nets": count},
            "publication_validation": "sqlite-version-clocks-row-counts-v1"}


def validate_prepared_timing_boundary(frontend, timing, clock_periods):
    """Validate an indexed Phase-2 publication without replaying STA.

    Prepared continuations predate the reusable experiment-checkpoint report
    schema. Their manifests and SQLite payloads are nevertheless a complete
    immutable Phase-2 boundary. Validate those native relations directly so
    later experiment stages can reuse them without materializing graph JSON or
    rerunning OpenSTA.
    """

    frontend, timing = Path(frontend).resolve(), Path(timing).resolve()
    phase1 = frontend / "phase1" if (frontend / "phase1").is_dir() else frontend
    source_ir = phase1 / "design.emuir.sqlite"
    if not source_ir.is_file():
        raise ValidationError("prepared Phase 1 indexed database is missing")
    control = read_compact_emuir_control_metadata(source_ir)
    design = control["design"]["name"]
    database = read_json(timing / "path-database.json")
    weights = read_json(timing / "partition-net-weights.json")
    if (
        database.get("schema") != "emuflow.sta-path-database/v3"
        or database.get("design") != design
        or weights.get("schema") != "emuflow.partition-net-weights/v2"
        or weights.get("design") != design
    ):
        raise ValidationError("prepared Phase 2 indexed identity mismatch")
    report = _published_timing_report(timing, database, weights, clock_periods)
    published_report_path = timing / "experiment-timing-report.json"
    if published_report_path.is_file():
        published_report = read_json(published_report_path)
        if (
            published_report.get("status") != "pass"
            or published_report.get("clocks") != dict(clock_periods)
        ):
            raise ValidationError(
                "prepared Phase 2 publication or clock constraints mismatch"
            )
    return report


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
    platform = Platform.load(platform_path)
    if _prepared_device_parts(phase1 / "platform.normalized.json") != {
        f.part for f in platform.fpgas
    }:
        raise ValidationError("prepared Phase 1 device part differs from the selected platform")
    database = read_json(timing / "path-database.json")
    if database.get("schema") != "emuflow.sta-path-database/v3" or database.get("design") != report["design"]:
        raise ValidationError("prepared Phase 2 must be the indexed database for this design")
    weights = read_json(timing / "partition-net-weights.json")
    timing_report_path = timing / "experiment-timing-report.json"
    timing_report = (
        read_json(timing_report_path)
        if timing_report_path.is_file()
        else _published_timing_report(
            timing, database, weights, clock_periods
        )
    )
    if (
        timing_report.get("status") != "pass"
        or timing_report.get("clocks") != dict(clock_periods)
    ):
        raise ValidationError(
            "prepared Phase 2 publication or clock constraints mismatch"
        )
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
    write_json(timing_out / "experiment-timing-report.json", timing_report, compact=True)
    bound_report = {**report, "platform": platform.name,
                    "continuation": {"mode": "read-only-indexed-boundary", "source": str(source_ir)}}
    write_json(phase1_out / "phase1_report.json", bound_report, compact=True)
    return bound_report, {
        "status": "pass", "mode": "validated-existing-phase2", "backend": "opensta",
        "sta": timing_report.get("sta", {}), "optimization_enabled": True,
        "partition_weights": timing_report.get("weights", {}),
    }
