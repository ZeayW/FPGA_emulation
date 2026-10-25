"""Current indexed Phase-6 implementation provider for AMD Vivado.

This path does not use the retired whole-EmuIR or sampled timing adapters.
It consumes the common mapped index, fixed-I/O contract and complete timing
queries, then publishes the existing provider-neutral physical result.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from .errors import EmuFlowError, ValidationError
from .indexed_physical_ir import IndexedPhysicalIR
from .io import file_sha256, read_json, write_json
from .physical_backend import PHYSICAL_PARTITION_RESULT_SCHEMA, validate_physical_partition_result
from .resource_budget import validate_device_resource_usage
from .store_publication import bind_store_source, require_store_sources_unchanged
from .vivado_physical_top import write_vivado_physical_top
from .vivado_lineage import LINEAGE_POLICY
from .vivado_optimization import OptimizationProofView
from .vivado_clocks import implementation_timing_contract, validate_dut_frequency_mhz

_ROOT = Path(__file__).resolve().parents[2]


def _vivado_script(name: str) -> Path:
    for directory in (_ROOT / "scripts/vivado", Path(sys.prefix) / "share/emuflow/scripts/vivado"):
        path = directory / name
        if path.is_file():
            return path
    raise EmuFlowError(f"installed Vivado Tcl script is missing: {name}")


def _retained_lineage_artifact(output: Path, coverage):
    path = output/"cell-lineage.sqlite"
    if (not path.is_file() or path.is_symlink()
            or Path(coverage.get("output", "")).resolve() != path.resolve()):
        raise ValidationError("Vivado routed lineage/placement owner is missing or misbound")
    binding = bind_store_source(path, "vivado-lineage-store-v2")
    digest = file_sha256(path)
    size = path.stat().st_size
    require_store_sources_unchanged((binding,))
    if digest != coverage.get("sha256") or size != coverage.get("bytes"):
        raise ValidationError("Vivado routed lineage/placement owner differs from its import seal")
    return {"path":str(path), "sha256":digest, "bytes":size}, binding


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
            "log_sha256": file_sha256(log_path), "script_sha256": file_sha256(script), "runtime_seconds": time.monotonic() - started,
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


def validate_synthesized_continuation(checkpoint: Path, expected_sha256: str, source_report: Path,
                                     mapped_report, mapped_verilog: Path, prepared_output: Path):
    """Bind one explicitly requested active-run continuation to frozen inputs."""
    if (not isinstance(expected_sha256,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_sha256)
            or file_sha256(checkpoint) != expected_sha256):
        raise ValidationError("synthesized continuation checkpoint digest differs")
    prior = read_json(source_report)
    for key in ("schema","status","provider","optimization_policy","top","emitted_instances","output_sha256"):
        if prior.get(key) != mapped_report.get(key):
            raise ValidationError(f"synthesized continuation source report differs: {key}")
    for key in ("mapped_index","source_functions"):
        if prior.get(key,{}).get("sha256") != mapped_report.get(key,{}).get("sha256"):
            raise ValidationError("synthesized continuation mapped/function identity differs")
        if file_sha256(Path(mapped_report[key]["path"])) != mapped_report[key]["sha256"]:
            raise ValidationError("synthesized continuation source payload digest differs")
    if file_sha256(mapped_verilog) != mapped_report["output_sha256"]:
        raise ValidationError("synthesized continuation Verilog digest differs")
    expected_files = ["runtime.xdc"]
    if (prepared_output/"physical_top.sv").is_file():
        expected_files.extend(["physical_top.sv","fixed-io-query.tsv","fixed-io-timing-map.tsv"])
    for name in expected_files:
        old, new = checkpoint.parent/name, prepared_output/name
        if not old.is_file() or file_sha256(old) != file_sha256(new):
            raise ValidationError(f"synthesized continuation runtime/fixed-I/O contract differs: {name}")
    return {"status":"pass","synthesized_checkpoint_sha256":expected_sha256,
            "source_report_sha256":file_sha256(source_report),"mapped_index_sha256":mapped_report["mapped_index"]["sha256"],
            "input_contracts":expected_files,"scope":"active-experiment synthesized-boundary continuation only"}


def run_indexed_vivado_partition(
    *, fpga: str, part: str, ir_path: Path, mapped_verilog_path: Path,
    mapped_report: Mapping[str, Any], runtime: Mapping[str, Any],
    original_cells: int, transport_cells: int, output_dir: Path,
    boundary_identity_path: Path | None = None, logic_identity_path: Path | None = None,
    logic_query_path: Path | None = None, local_identity_path: Path | None = None,
    local_query_path: Path | None = None, virtual_io_contract: Mapping[str, Any] | None = None,
    executable: str | None = None, place_directive: str = "Default", route_directive: str = "Default",
    synthesized_checkpoint: Path | None = None, synthesized_checkpoint_sha256: str | None = None,
    continuation_source_report: Path | None = None, dut_frequency_mhz: float | None = None,
    word_phy_plan_path:Path|None=None,word_wiring_path:Path|None=None,
) -> dict[str, Any]:
    from .vivado_backend import _resolve_vivado, _read_metrics, _integer, _number, vivado_runtime_xdc, write_vivado_boundary_timing_query
    from .vivado_physical_timing import import_indexed_vivado_boundary, import_vivado_local_timing
    from .logic_segment_timing import import_vivado_logic_segment_timing
    from .vivado_session import prepare_manifest

    continuation = (synthesized_checkpoint,synthesized_checkpoint_sha256,continuation_source_report)
    word_mode=word_phy_plan_path is not None or word_wiring_path is not None
    if word_mode and (word_phy_plan_path is None or word_wiring_path is None or virtual_io_contract is not None):
        raise ValidationError('native word implementation requires its plan and wiring together, without a bit-adapter contract')
    if word_mode and any(value is not None for value in continuation):
        raise ValidationError('native word synthesized continuation requires a dedicated source/IP admission contract')
    if any(value is not None for value in continuation) and not all(value is not None for value in continuation):
        raise ValidationError("synthesized continuation requires checkpoint, digest and source report together")
    from .emuir_storage import is_emuir_sqlite
    from .emuir_compact import is_compact_emuir

    if not is_emuir_sqlite(ir_path) or not is_compact_emuir(ir_path):
        raise ValidationError("Vivado physical implementation requires current indexed EmuIR")
    if not re.fullmatch(r"xc[a-z0-9-]+", part):
        raise ValidationError("Vivado requires a concrete Xilinx part")
    for kind, identity, query in (("logic", logic_identity_path, logic_query_path),
                                  ("local", local_identity_path, local_query_path)):
        if (identity is None) != (query is None):
            raise ValidationError(f"Vivado {kind} timing requires identity and query together")
    validate_dut_frequency_mhz(dut_frequency_mhz)
    implementation_timing = implementation_timing_contract(runtime, dut_frequency_mhz)
    from .native_tools import require_native_schema
    require_native_schema("vivado-lineage-store-v2")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any((output_dir/name).exists() for name in ("native-session.json","mapped_cells.tsv","vivado-implementation.log")):
        raise ValidationError("indexed Vivado implementation requires a fresh native output directory")
    executable = _resolve_vivado(executable)
    index_path = Path(mapped_report["mapped_index"]["path"])
    if mapped_report.get("optimization_policy") != LINEAGE_POLICY:
        raise ValidationError("indexed Vivado requires the current optimization/lineage policy")
    functions_path = Path(mapped_report["source_functions"]["path"])
    expected = original_cells + transport_cells
    with IndexedPhysicalIR(ir_path) as ir:
        if len(ir.instances) != expected or mapped_report["emitted_instances"] != expected:
            raise ValidationError("Vivado input mapped cell accounting disagrees")
        top = ir.value["design"]["top"]
        ports = {port["id"] for port in ir.value["ports"]}
    dut_count = sum(clock in ports for clock in runtime["timing_model"]["dut_clock_ports"])
    xdc = output_dir / "runtime.xdc"
    xdc.write_text(vivado_runtime_xdc(ir_path, runtime, dut_frequency_mhz), encoding="utf-8")
    prefix, adapter, wrapper, io_xdc, io_query = "", "", "", "", ""
    top_report = None
    word_sources=()
    if word_mode:
        from .vivado_word_top import write_vivado_word_top
        for path in (word_phy_plan_path,word_wiring_path):
            if not path.is_file()or path.stat().st_size>16*1024*1024:
                raise ValidationError('native word plan/wiring source is missing or exceeds its bounded board scope')
        word_sources=tuple(bind_store_source(path,'native-word-physical-source-v1')for path in (word_phy_plan_path,word_wiring_path))
        top_report=write_vivado_word_top(ir_path,output_dir,fpga=fpga,
            board=read_json(word_wiring_path),plan=read_json(word_phy_plan_path))
        if top_report['part']!=part or implementation_timing['tdm_clock']['period_ns']!=5.0:
            raise ValidationError('native word part or TDM clock differs from its PHY model')
        write_json(output_dir/'native-word-preparation.json',top_report,compact=True)
        top,prefix=top_report['top'],top_report['hierarchy_prefix']
        adapter,wrapper=str(output_dir/'native-word-banks.sv'),str(output_dir/'physical_top.sv')
        io_xdc,io_query=str(output_dir/'native-word-fixed-io.xdc'),str(output_dir/'fixed-io-query.tsv')
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
    if word_mode:
        io_timing_map.write_bytes((output_dir/'native-word-boundaries.tsv').read_bytes())
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
    queries = {}
    boundary_query_report = None
    if boundary_identity_path is not None:
        query = output_dir/"boundary-query.tsv"
        boundary_query_report = write_vivado_boundary_timing_query(ir_path,boundary_identity_path,query)
        queries["boundary_timing"] = query
    for kind,query in (("logic_segment_timing",logic_query_path),("local_path_timing",local_query_path)):
        if query is not None:
            queries[kind] = query
    continuation_validation = None
    if synthesized_checkpoint is not None:
        if synthesized_checkpoint.resolve().parent == output_dir:
            raise ValidationError("synthesized continuation must use a fresh native output directory")
        continuation_validation = validate_synthesized_continuation(synthesized_checkpoint,synthesized_checkpoint_sha256,
            continuation_source_report,mapped_report,mapped_verilog_path,output_dir)
    configuration = prepare_manifest(output_dir,fpga=fpga,part=part,mapped_index=index_path,functions=functions_path,
        mapped_sha256=mapped_report["mapped_index"]["sha256"],queries=queries,runtime=runtime,ports=ports,
        synthesized_checkpoint=synthesized_checkpoint,synthesized_checkpoint_sha256=synthesized_checkpoint_sha256,
        dut_frequency_mhz=dut_frequency_mhz,word_phy_directory=output_dir if word_mode else None)
    threads = int(os.environ.get("EMUFLOW_VIVADO_THREADS", "2"))
    if threads < 1:
        raise ValidationError("Vivado thread count must be positive")
    input_files = {"physical_ir":ir_path,"mapped_index":index_path,"source_functions":functions_path,
                   "mapped_verilog":mapped_verilog_path,**{f"query_{kind}":path for kind,path in queries.items()}}
    if word_mode:input_files.update(word_phy_plan=word_phy_plan_path,word_wiring=word_wiring_path)
    input_identity = {key:{"path":str(path.resolve()),"sha256":file_sha256(path)} for key,path in input_files.items()}
    input_identity["runtime_sha256"] = hashlib.sha256(json.dumps(runtime,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    for key,expected_sha in (("mapped_index",mapped_report["mapped_index"]["sha256"]),
                              ("source_functions",mapped_report["source_functions"]["sha256"]),
                              ("mapped_verilog",mapped_report["output_sha256"])):
        if input_identity[key]["sha256"] != expected_sha:
            raise ValidationError("Vivado mapped source identity differs before native execution")
    implementation = execute_vivado(executable, _vivado_script("implement_indexed_partition.tcl"),
        [part, str(mapped_verilog_path.resolve()), top, str(xdc), str(output_dir), str(expected), prefix,
         adapter, wrapper, io_xdc, io_query, place_directive, route_directive, str(dut_count), str(threads), str(configuration)],
        output_dir, "vivado-implementation.log")
    metrics = _read_metrics(output_dir / "implementation_metrics.tsv")
    if metrics.get("part") != part or _integer(metrics, "dut_clock_count") != dut_count:
        raise ValidationError("Vivado part/clock identity differs from Phase6")
    preparation = read_json(output_dir/"routed-query-preparation.json")
    if preparation.get("status") != "pass":
        raise ValidationError("native routed query preparation did not pass")
    coverage, proof_report = preparation["coverage"], preparation["optimization_proofs"]
    lineage_artifact, lineage_binding = _retained_lineage_artifact(output_dir, coverage)
    proof_path = output_dir/"optimization-proofs.sqlite"
    if coverage["stages"]["routed"]["placed_objects"] != _integer(metrics, "routed_cells"):
        raise ValidationError("Vivado lineage placement coverage differs from physical mapped cells")
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
    export = {"status":"pass","mode":"current-routed-native-session","log":implementation["log"]}
    if boundary_identity_path is not None:
        measured = output_dir/"boundary_timing.tsv"
        with OptimizationProofView(proof_path, expected_fpga=fpga) as proof_reader:
            imported = import_indexed_vivado_boundary(measured, boundary_identity_path, output_dir / "boundary-timing.json",
                measurement_scope="native-word-registered-interface" if word_mode else("board-integrated-interface" if virtual_io_contract is not None else "partition-boundary"),
                optimization_proofs=proof_reader)
        measured.unlink()
        stages["boundary_timing"] = {"status":"pass","query":boundary_query_report,
            "resolution":preparation["resolutions"]["boundary_timing"],"export":export,"import":imported}
    for kind, identity, query, importer in (
        ("logic_segment_timing", logic_identity_path, logic_query_path, import_vivado_logic_segment_timing),
        ("local_path_timing", local_identity_path, local_query_path, import_vivado_local_timing),
    ):
        if (identity is None) != (query is None):
            raise ValidationError(f"Vivado {kind} requires identity and query together")
        if identity is None:
            continue
        measured = output_dir / f"{kind}.tsv"
        resolution = preparation["resolutions"][kind]
        with OptimizationProofView(proof_path, expected_fpga=fpga) as proof_reader:
            imported = importer(measured, identity, output_dir / f"{kind}.json", optimization_proofs=proof_reader)
        measured.unlink()
        stages[kind] = {"status": "pass", "resolution": resolution, "export": export, "import": imported}
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
    if word_mode:
        pairs=_integer(metrics,'virtual_io_pairs')
        if pairs!=top_report['data_pairs']+top_report['forwarded_clock_pairs']:
            raise ValidationError('native word fixed pair count differs from its data/clock bank plan')
        word_checks=_integer(metrics,'native_word_infrastructure_checks')
        word_wns=_number(metrics,'native_word_infrastructure_wns_ns')if word_checks else None
        if (top_report['active_banks']and not word_checks)or(word_wns is not None and word_wns<0):
            raise ValidationError('native word infrastructure lacks passing fixed-clock timing evidence')
        require_store_sources_unchanged(word_sources)
    require_store_sources_unchanged((lineage_binding,))
    artifacts = {"cell-lineage.sqlite":lineage_artifact}
    for name in ("route_status.rpt", "drc.rpt", "timing_summary.rpt", "utilization.rpt", "implementation_metrics.tsv",
                 "native-session.json", "native-session.tcl", "clock-contract.tsv",
                 "routed-query-preparation.json", "constant-frontier-validation.json"):
        path = output_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError(f"Vivado physical artifact is missing: {name}")
        artifacts[name] = {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
    if word_mode:
        for name in ('native-word-preparation.json','native-word-clock-census.tsv'):
            path=output_dir/name
            artifacts[name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
    result = {"schema": PHYSICAL_PARTITION_RESULT_SCHEMA, "status": "pass",
        "identity": {"backend": "vivado", "fpga": fpga, "part": part},
        "analysis_scope": {"implementation_mode": "out-of-context",
            "data_path_delay": "routed-vendor-device",
            "external_clock_sources": "abstract-unbound",
            "absolute_clock_insertion_and_skew": "not-qualified",
            "cell_accounting_policy": LINEAGE_POLICY,
            "implementation_clock_policy": implementation_timing["policy"],
            "board_signoff": False},
        "cell_accounting": {"original_cells": original_cells, "transport_cells": transport_cells,
            **{name: coverage["stages"]["routed"][name] for name in ("source_cells_with_lineage", "unretained_source_cells", "source_cells_with_multiple_objects")},
            **{name: _integer(metrics, name) for name in ("routed_cells", "physical_cells", "infrastructure_cells", "optimization_cells")}},
        "closure": {name: _integer(metrics, name) for name in ("unrouted_nets", "drc_violations", "drc_warnings")},
        "clocks": {"fabric_period_ns": _number(metrics, "fabric_period_ns"),
            "dut_period_ns": min(implementation_timing["dut_clock_periods_ns"].values()),
            "dut_clock_periods_ns": {port: period for port, period in implementation_timing["dut_clock_periods_ns"].items() if port in ports},
            "virtual_runtime_dut_clock_periods_ns": {port: runtime["virtual_dut_clocks"][port]["nominal_period_ns"]
                for port in implementation_timing["dut_clock_periods_ns"]}},
        "timing": {**aggregate, "timing_met": aggregate["wns_ns"] >= 0,
            **{name: _number(metrics, name) for name in ("dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns", "critical_path_ns")},
            "clock_domain_delays_ns": {domain: _number(metrics, name) for domain, name in (("overall", "critical_path_ns"), ("dut", "dut_delay_ns"), ("fabric", "fabric_delay_ns"), ("cross", "fabric_to_dut_delay_ns"))}},
        "resource_usage": usage, "hard_resources": counts, "artifacts": artifacts}
    validation = validate_physical_partition_result(result, backend="vivado", fpga=fpga, part=part, original_cells=original_cells, transport_cells=transport_cells)
    native_scripts=("native_session.tcl","source_inventory.tcl","resource_inventory.tcl","timing_contract.tcl",
        "inspect_constant_frontiers.tcl","export_boundary_timing.tcl","export_logic_segment_timing.tcl")
    if word_mode:native_scripts+=("word_native_physical.tcl","selectio_ip.tcl")
    report = {"schema": "emuflow.vivado-partition-report/v1", "status": "pass", "provider": "indexed-phase6-vivado-physical-v2",
        "tool": {"executable": executable, "version": metrics["vivado_version"], "threads": threads},
        "implementation": implementation, "continuation": continuation_validation, "input_identity": input_identity,
        "implementation_timing": implementation_timing,
        "implementation_options": {"place_directive": place_directive, "route_directive": route_directive,
                                   "dut_frequency_mhz": dut_frequency_mhz},
        "native_session": {"mode":"current-design-stage-exports-v1",
            "scripts_sha256": {name:file_sha256(_vivado_script(name)) for name in native_scripts},
            "planner_sha256":file_sha256(Path(__file__).with_name("vivado_session.py"))},
        "cell_coverage": coverage, "optimization_proofs": proof_report, **stages,
        **({"virtual_io_implementation": virtual_io} if virtual_io is not None else {}),
        **({'word_phy_implementation':{'status':'pass','preparation':top_report,
            'infrastructure_timing':{'checks':word_checks,'wns_ns':word_wns,'fixed_clock_contract_met':True},
            'differential_pairs':pairs,'serial_pad_timing_scope':'fixed-link-model-external-to-local-STA; no board/SI signoff'}}if word_mode else{}),
        "result": result, "validation": validation}
    write_json(output_dir / "vivado-partition-report.json", report, compact=True)
    return report
