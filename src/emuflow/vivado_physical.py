"""Current indexed Phase-6 implementation provider for AMD Vivado.

This path does not use the retired whole-EmuIR or sampled timing adapters.
It consumes the common mapped index, fixed-I/O contract and complete timing
queries, then publishes the existing provider-neutral physical result.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from .errors import EmuFlowError, ValidationError
from .indexed_physical_ir import IndexedPhysicalIR
from .io import file_sha256, write_json
from .physical_backend import PHYSICAL_PARTITION_RESULT_SCHEMA, validate_physical_partition_result
from .resource_budget import validate_device_resource_usage
from .vivado_physical_top import write_vivado_physical_top

_ROOT = Path(__file__).resolve().parents[2]


def _vivado_script(name: str) -> Path:
    for directory in (_ROOT / "scripts/vivado", Path(sys.prefix) / "share/emuflow/scripts/vivado"):
        path = directory / name
        if path.is_file():
            return path
    raise EmuFlowError(f"installed Vivado Tcl script is missing: {name}")


def execute_vivado(executable: str, script: Path, arguments: list[str], output: Path, name: str) -> dict[str, Any]:
    """Stream provider output to scratch instead of buffering the full log."""
    log_path = output / name
    scratch = output / ".tmp"
    scratch.mkdir(exist_ok=True)
    command = [executable, "-mode", "batch", "-nojournal", "-nolog", "-notrace", "-source", str(script), "-tclargs", *arguments]
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=output, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   env={**os.environ, "TMPDIR": str(scratch), "XILINX_LOCAL_USER_DATA": "no"}, check=False)
    tail: deque[str] = deque(maxlen=40)
    critical = 0
    with log_path.open(encoding="utf-8", errors="replace") as log:
        for line in log:
            tail.append(line.rstrip())
            critical += "CRITICAL WARNING:" in line
    if completed.returncode or critical:
        raise EmuFlowError(f"Vivado {script.name} failed: exit={completed.returncode}, critical_warnings={critical}\n" + "\n".join(tail))
    return {"status": "pass", "command": command, "log": str(log_path),
            "log_sha256": file_sha256(log_path), "runtime_seconds": time.monotonic() - started,
            "critical_warnings": 0}


def parse_timing_summary(path: Path) -> dict[str, Any]:
    """Read the vendor's complete endpoint aggregate, never a top-N sample."""
    armed = False
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if "WNS(ns)" in line and "TNS(ns)" in line:
                armed = True
                continue
            if not armed:
                continue
            fields = line.split()
            if len(fields) < 8:
                continue
            try:
                wns, tns = float(fields[0]), float(fields[1])
                failures, total = int(fields[2]), int(fields[3])
            except ValueError:
                continue
            if (not all(math.isfinite(x) for x in (wns, tns)) or tns > 0
                    or not 0 <= failures <= total or (tns < 0) != (failures > 0)):
                raise ValidationError("Vivado endpoint timing aggregate is invalid")
            return {"wns_ns": wns, "tns_ns": tns, "failing_endpoints": failures,
                    "failing_endpoint_constraints": failures, "analyzed_endpoints": total}
    raise ValidationError("Vivado timing summary has no complete setup aggregate")


def validate_indexed_cell_coverage(mapped_path: Path, inventories: Mapping[str, Path]) -> dict[str, Any]:
    """Join streamed tool inventories to the canonical cell owner on disk."""
    with tempfile.TemporaryDirectory(prefix=".vivado-coverage-", dir=mapped_path.parent) as temporary:
        connection = sqlite3.connect(Path(temporary) / "coverage.sqlite", uri=True)
        try:
            connection.execute("ATTACH DATABASE ? AS mapped", (f"file:{mapped_path.resolve()}?mode=ro&immutable=1",))
            expected = int(connection.execute("SELECT count(*) FROM mapped.cells").fetchone()[0])
            counts = {}
            for stage, path in inventories.items():
                connection.execute("DROP TABLE IF EXISTS actual")
                connection.execute("CREATE TABLE actual(name TEXT PRIMARY KEY,ref TEXT NOT NULL)")
                with path.open(encoding="utf-8", newline="") as stream:
                    rows = csv.reader(stream, delimiter="\t")
                    if next(rows, None) != ["name", "ref_name"]:
                        raise ValidationError("Vivado cell inventory header is invalid")
                    try:
                        for row in rows:
                            if len(row) != 2 or not all(row):
                                raise ValidationError("Vivado cell inventory row is invalid")
                            connection.execute("INSERT INTO actual VALUES (?,?)", row)
                    except sqlite3.IntegrityError as error:
                        raise ValidationError("Vivado cell inventory contains duplicate identities") from error
                counts[stage] = int(connection.execute("SELECT count(*) FROM actual").fetchone()[0])
                # Vivado doubles embedded backslashes in its NAME property.
                missing = connection.execute("SELECT 1 FROM mapped.cells c LEFT JOIN actual a ON a.name=replace(c.id,char(92),char(92)||char(92)) WHERE a.name IS NULL OR a.ref != c.type LIMIT 1").fetchone()
                if counts[stage] != expected or missing is not None:
                    raise ValidationError(f"Vivado {stage} mapped cell identity/type coverage disagrees")
            return {"status": "pass", "provider": "indexed-mapped-cell-identity-v1", "logical_cells": expected, **counts}
        finally:
            connection.close()


def run_indexed_vivado_partition(
    *, fpga: str, part: str, ir_path: Path, mapped_verilog_path: Path,
    mapped_report: Mapping[str, Any], runtime: Mapping[str, Any],
    original_cells: int, transport_cells: int, output_dir: Path,
    boundary_identity_path: Path | None = None, logic_identity_path: Path | None = None,
    logic_query_path: Path | None = None, local_identity_path: Path | None = None,
    local_query_path: Path | None = None, virtual_io_contract: Mapping[str, Any] | None = None,
    executable: str | None = None, place_directive: str = "Default", route_directive: str = "Default",
) -> dict[str, Any]:
    from .vivado_backend import _resolve_vivado, _read_metrics, _integer, _number, vivado_runtime_xdc, write_vivado_boundary_timing_query
    from .vivado_physical_timing import import_indexed_vivado_boundary, import_vivado_local_timing
    from .logic_segment_timing import import_vivado_logic_segment_timing

    if not re.fullmatch(r"xc[a-z0-9-]+", part):
        raise ValidationError("Vivado requires a concrete Xilinx part")
    for kind, identity, query in (("logic", logic_identity_path, logic_query_path),
                                  ("local", local_identity_path, local_query_path)):
        if (identity is None) != (query is None):
            raise ValidationError(f"Vivado {kind} timing requires identity and query together")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = _resolve_vivado(executable)
    index_path = Path(mapped_report["mapped_index"]["path"])
    expected = original_cells + transport_cells
    with IndexedPhysicalIR(ir_path) as ir:
        if len(ir.instances) != expected or mapped_report["emitted_instances"] != expected:
            raise ValidationError("Vivado input mapped cell accounting disagrees")
        top = ir.value["design"]["top"]
        ports = {port["id"] for port in ir.value["ports"]}
    dut_count = sum(clock in ports for clock in runtime["timing_model"]["dut_clock_ports"])
    xdc = output_dir / "runtime.xdc"
    xdc.write_text(vivado_runtime_xdc(ir_path, runtime), encoding="utf-8")
    prefix, adapter, wrapper, io_xdc, io_query = "", "", "", "", ""
    top_report = None
    if virtual_io_contract is not None:
        wrapper_path = output_dir / "physical_top.sv"
        top_report = write_vivado_physical_top(ir_path, wrapper_path, virtual_io_contract)
        top, prefix = top_report["top"], top_report["hierarchy_prefix"]
        adapter = str(Path(virtual_io_contract["adapter"]["path"]).resolve())
        wrapper, io_xdc = str(wrapper_path), str(Path(virtual_io_contract["xdc"]["path"]).resolve())
        query_path = output_dir / "fixed-io-query.tsv"
        with query_path.open("w", encoding="utf-8") as stream:
            stream.write("direction\tp_hex\tn_hex\tp_pin_hex\tn_pin_hex\tiostandard_hex\n")
            for entry in virtual_io_contract["entries"]:
                values = [entry["p"]["port"], entry["n"]["port"], entry["p"]["package_pin"], entry["n"]["package_pin"], entry["iostandard"]]
                stream.write("\t".join([entry["direction"], *(value.encode("utf-8").hex() for value in values)]) + "\n")
        io_query = str(query_path)
    io_timing_map = output_dir / "fixed-io-timing-map.tsv"
    if virtual_io_contract is not None:
        with io_timing_map.open("w", encoding="utf-8") as stream:
            stream.write("logical_hex\tphysical_hex\n")
            for entry in virtual_io_contract["entries"]:
                logical = f"{entry['logical_port']}[{entry['logical_bit']}]"
                names = [logical]
                if entry["logical_port_width"] == 1:
                    names.append(entry["logical_port"])
                for name in names:
                    stream.write(name.encode().hex() + "\t" + entry["p"]["port"].encode().hex() + "\n")
    timing_arguments = [prefix] + ([str(io_timing_map)] if virtual_io_contract is not None else [])
    threads = int(os.environ.get("EMUFLOW_VIVADO_THREADS", "2"))
    if threads < 1:
        raise ValidationError("Vivado thread count must be positive")
    implementation = execute_vivado(executable, _vivado_script("implement_indexed_partition.tcl"),
        [part, str(mapped_verilog_path.resolve()), top, str(xdc), str(output_dir), str(expected), prefix,
         adapter, wrapper, io_xdc, io_query, place_directive, route_directive, str(dut_count), str(threads)],
        output_dir, "vivado-implementation.log")
    metrics = _read_metrics(output_dir / "implementation_metrics.tsv")
    if metrics.get("part") != part or _integer(metrics, "dut_clock_count") != dut_count:
        raise ValidationError("Vivado part/clock identity differs from Phase6")
    coverage = validate_indexed_cell_coverage(index_path, {
        "synthesized": output_dir / "mapped_cells.tsv", "routed": output_dir / "routed_mapped_cells.tsv"})
    aggregate = parse_timing_summary(output_dir / "timing_summary.rpt")
    if abs(aggregate["wns_ns"] - _number(metrics, "wns_ns")) > 0.002:
        raise ValidationError("Vivado timing aggregate and worst path disagree")
    # report_route_status also covers partially routed/conflicting nets that
    # must not be accepted merely because the UNROUTED filter is empty.
    with (output_dir / "route_status.rpt").open(encoding="utf-8") as stream:
        for line in stream:
            if any(label in line.lower() for label in ("nets with routing errors", "unrouted nets", "partially routed nets")):
                match = re.search(r"([0-9]+)\s*$", line)
                if match and int(match.group(1)):
                    raise ValidationError("Vivado route-status report contains incomplete routing")
    counts = {field: _integer(metrics, field) for field in ("lut", "ff", "carry8", "dsp48", "uram288")}
    counts["bram18k"] = _integer(metrics, "ramb18_cells") + 2 * _integer(metrics, "ramb36_cells")
    usage = {}
    if "resource_budget" in runtime:
        usage = {"physical": validate_device_resource_usage(counts, runtime["resource_budget"], stage="post-physical-materialization")}
    stages = {}
    if boundary_identity_path is not None:
        query = output_dir / "boundary-query.tsv"
        query_report = write_vivado_boundary_timing_query(ir_path, boundary_identity_path, query)
        measured = output_dir / "boundary-timing.tsv"
        export = execute_vivado(executable, _vivado_script("export_boundary_timing.tcl"),
                                [str(output_dir / "routed.dcp"), str(query), str(measured), *timing_arguments], output_dir, "boundary-export.log")
        imported = import_indexed_vivado_boundary(measured, boundary_identity_path, output_dir / "boundary-timing.json",
            measurement_scope="board-integrated-interface" if virtual_io_contract is not None else "partition-boundary")
        stages["boundary_timing"] = {"status": "pass", "query": query_report, "export": export, "import": imported}
    for kind, identity, query, importer in (
        ("logic_segment_timing", logic_identity_path, logic_query_path, import_vivado_logic_segment_timing),
        ("local_path_timing", local_identity_path, local_query_path, import_vivado_local_timing),
    ):
        if (identity is None) != (query is None):
            raise ValidationError(f"Vivado {kind} requires identity and query together")
        if identity is None:
            continue
        measured = output_dir / f"{kind}.tsv"
        export = execute_vivado(executable, _vivado_script("export_logic_segment_timing.tcl"),
                                [str(output_dir / "routed.dcp"), str(query), str(measured), *timing_arguments], output_dir, f"{kind}-export.log")
        imported = importer(measured, identity, output_dir / f"{kind}.json")
        stages[kind] = {"status": "pass", "export": export, "import": imported}
    virtual_io = None
    if virtual_io_contract is not None:
        pairs = _integer(metrics, "virtual_io_pairs")
        if pairs != virtual_io_contract["metrics"]["differential_pairs"]:
            raise ValidationError("Vivado implemented fixed differential pair count disagrees")
        virtual_io = {"status": "pass", "provider": "vivado-fixed-lvds-routed-boundary-v1",
            "qualification": "routed-IBUFDS-OBUFDS-with-fixed-P-N-package-pins-and-IOSTANDARD",
            "contract_sha256": hashlib.sha256(json.dumps(virtual_io_contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "differential_pairs": pairs, "physical_ports": 2 * pairs,
            "top": top_report, "route_validation": {"unrouted_nets": _integer(metrics, "unrouted_nets")}}
    artifacts = {}
    for name in ("synthesized.dcp", "placed.dcp", "routed.dcp", "route_status.rpt", "drc.rpt", "timing_summary.rpt", "utilization.rpt", "implementation_metrics.tsv"):
        path = output_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError(f"Vivado physical artifact is missing: {name}")
        artifacts[name] = {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
    result = {"schema": PHYSICAL_PARTITION_RESULT_SCHEMA, "status": "pass",
        "identity": {"backend": "vivado", "fpga": fpga, "part": part},
        "analysis_scope": {"implementation_mode": "out-of-context",
            "data_path_delay": "routed-vendor-device",
            "external_clock_sources": "abstract-unbound",
            "absolute_clock_insertion_and_skew": "not-qualified",
            "board_signoff": False},
        "cell_accounting": {"original_cells": original_cells, "transport_cells": transport_cells,
            "routed_cells": expected, **{name: _integer(metrics, name) for name in ("physical_cells", "infrastructure_cells", "optimization_cells")}},
        "closure": {name: _integer(metrics, name) for name in ("unrouted_nets", "drc_violations", "drc_warnings")},
        "clocks": {"fabric_period_ns": _number(metrics, "fabric_period_ns"), "dut_period_ns": float(runtime["virtual_dut_clock"]["nominal_period_ns"])},
        "timing": {**aggregate, "timing_met": aggregate["wns_ns"] >= 0,
            **{name: _number(metrics, name) for name in ("dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns", "critical_path_ns")},
            "clock_domain_delays_ns": {domain: _number(metrics, name) for domain, name in (("overall", "critical_path_ns"), ("dut", "dut_delay_ns"), ("fabric", "fabric_delay_ns"), ("cross", "fabric_to_dut_delay_ns"))}},
        "resource_usage": usage, "hard_resources": counts, "artifacts": artifacts}
    validation = validate_physical_partition_result(result, backend="vivado", fpga=fpga, part=part, original_cells=original_cells, transport_cells=transport_cells)
    report = {"schema": "emuflow.vivado-partition-report/v1", "status": "pass", "provider": "indexed-phase6-vivado-physical-v1",
        "tool": {"executable": executable, "version": metrics["vivado_version"], "threads": threads},
        "implementation": implementation, "cell_coverage": coverage, **stages,
        **({"virtual_io_implementation": virtual_io} if virtual_io is not None else {}),
        "result": result, "validation": validation}
    write_json(output_dir / "vivado-partition-report.json", report, compact=True)
    return report
