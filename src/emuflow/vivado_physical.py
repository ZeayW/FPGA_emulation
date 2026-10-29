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
from .vivado_clocks import implementation_timing_contract, validate_dut_frequency_mhz
from .sampling_cost import SAMPLING_COST_POLICY

_ROOT = Path(__file__).resolve().parents[2]
WORD_CONTROL_TIMING_SCOPE = "registered-control-paths-excluding-rx-shadow-launches-v1"


def _word_infrastructure_timing(metrics, *, active_banks):
    from .vivado_backend import _integer, _number
    if metrics.get("native_word_infrastructure_scope") != WORD_CONTROL_TIMING_SCOPE:
        raise ValidationError("native word control timing lacks its routed source classification")
    checks = _integer(metrics, "native_word_infrastructure_checks")
    wns = _number(metrics, "native_word_infrastructure_wns_ns") if checks else None
    whs = _number(metrics, "native_word_infrastructure_whs_ns") if checks else None
    complete = _integer(metrics, "native_word_rx_scope_complete")
    if complete not in (0, 1):
        raise ValidationError("native word RX scope completeness is invalid")
    if complete and ((active_banks and checks != 2) or
                     (wns is not None and (wns < 0 or whs < 0))):
        raise ValidationError("native word registered controls lack passing fixed-clock timing evidence")
    return {"status":"complete" if complete else "incomplete", "scope":WORD_CONTROL_TIMING_SCOPE,
        "checks":checks, "wns_ns":wns, "whs_ns":whs, "rx_scope_complete":bool(complete),
        "excluded_rx_clock_pins":_integer(metrics, "native_word_rx_clock_pins_excluded"),
        "control_clock_pins":_integer(metrics, "native_word_control_clock_pins"),
        "fixed_clock_contract_met":True if complete else None}


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


def inspect_vivado_log(log_path: Path, returncode: int, script_name: str) -> dict[str, int]:
    """Keep query-size advice visible without rejecting completed results."""
    tail: deque[str] = deque(maxlen=40)
    critical = 0
    query_scale = 0
    with log_path.open(encoding="utf-8", errors="replace") as log:
        for line in log:
            tail.append(line.rstrip())
            critical += "CRITICAL WARNING:" in line
            # This warning concerns the size of a native collection. The
            # query can still complete with full coverage and valid results.
            query_scale += bool(re.match(r"^\s*CRITICAL WARNING:\s*\[Vivado 12-4439\]", line))
    blocking = critical-query_scale
    if returncode or blocking:
        raise EmuFlowError(f"Vivado {script_name} failed: exit={returncode}, critical_warnings={critical}\n" + "\n".join(tail))
    return {"critical_warnings":critical, "blocking_critical_warnings":blocking,
            "query_scale_advisories":query_scale}


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
    diagnostics = inspect_vivado_log(log_path, completed.returncode, script.name)
    return {"status": "pass", "command": command, "log": str(log_path),
            "log_sha256": file_sha256(log_path), "script_sha256": file_sha256(script), "runtime_seconds": time.monotonic() - started,
            **diagnostics}


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
    return _validate_snapshot_inputs(checkpoint,expected_sha256,source_report,mapped_report,
        mapped_verilog,prepared_output,stage='synthesized')


def _validate_snapshot_inputs(checkpoint: Path, expected_sha256: str, source_report: Path,
                                     mapped_report, mapped_verilog: Path, prepared_output: Path, *, stage):
    """Prove a retained implementation stage has the same compiled inputs."""
    if (not isinstance(expected_sha256,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_sha256)
            or file_sha256(checkpoint) != expected_sha256):
        raise ValidationError(f"{stage} continuation checkpoint digest differs")
    prior = read_json(source_report)
    for key in ("schema","status","provider","optimization_policy","top","emitted_instances","output_sha256"):
        if prior.get(key) != mapped_report.get(key):
            raise ValidationError(f"{stage} continuation source report differs: {key}")
    for key in ("mapped_index","source_functions"):
        if prior.get(key,{}).get("sha256") != mapped_report.get(key,{}).get("sha256"):
            raise ValidationError(f"{stage} continuation mapped/function identity differs")
        if file_sha256(Path(mapped_report[key]["path"])) != mapped_report[key]["sha256"]:
            raise ValidationError(f"{stage} continuation source payload digest differs")
    if file_sha256(mapped_verilog) != mapped_report["output_sha256"]:
        raise ValidationError(f"{stage} continuation Verilog digest differs")
    expected_files = ["runtime.xdc"]
    if (prepared_output/"physical_top.sv").is_file():
        expected_files.extend(["physical_top.sv","fixed-io-query.tsv","fixed-io-timing-map.tsv"])
    word_reports=[root/'native-word-preparation.json'for root in (checkpoint.parent,prepared_output)]
    if any(path.exists()for path in word_reports):
        from .vivado_word_top import SCHEMA as WORD_TOP_SCHEMA,WORD_TOP_FILES
        if not all(path.is_file()and path.stat().st_size<=1024*1024 for path in word_reports):
            raise ValidationError('native word continuation requires both bounded preparation seals')
        old_word,new_word=(read_json(path)for path in word_reports)
        fields=('schema','status','fpga','part','top','native_phy','source_board_sha256','source_word_plan_sha256','external_services')
        if any(old_word.get(key)!=new_word.get(key)for key in fields)or old_word.get('schema')!=WORD_TOP_SCHEMA:
            raise ValidationError('native word continuation PHY/source preparation differs')
        old_artifacts,new_artifacts=old_word['artifacts'],new_word['artifacts']
        if set(old_artifacts)!=set(new_artifacts)or not set(WORD_TOP_FILES)<=set(new_artifacts)or not len(new_artifacts)<=64:
            raise ValidationError('native word continuation source-file population differs')
        for name in sorted(new_artifacts):
            if Path(name).name!=name or old_artifacts[name]['sha256']!=new_artifacts[name]['sha256']:
                raise ValidationError('native word continuation input seal differs: '+name)
            for root,owner in ((checkpoint.parent,old_artifacts),(prepared_output,new_artifacts)):
                if not(root/name).is_file()or file_sha256(root/name)!=owner[name]['sha256']:
                    raise ValidationError('native word continuation input file differs: '+name)
            if name not in expected_files:expected_files.append(name)
    for name in expected_files:
        old, new = checkpoint.parent/name, prepared_output/name
        if not old.is_file() or file_sha256(old) != file_sha256(new):
            raise ValidationError(f"{stage} continuation runtime/fixed-I/O contract differs: {name}")
    return {"status":"pass",stage+"_checkpoint_sha256":expected_sha256,
            "source_report_sha256":file_sha256(source_report),"mapped_index_sha256":mapped_report["mapped_index"]["sha256"],
            "input_contracts":expected_files,"scope":f"retained {stage} snapshot with identical compiled inputs"}


def _require_same_word_data_origins(left: Path,right: Path):
    """Budget source sets have no ordering semantics."""
    populations=[]
    for path in (left,right):
        rows=path.read_text().splitlines()
        if(not rows or rows[0]!='source_origin'or len(set(rows[1:]))!=len(rows)-1 or
           any(re.fullmatch(r's[0-9]+',value)is None for value in rows[1:])):
            raise ValidationError('retained routed RX budget source roles are malformed')
        populations.append(set(rows[1:]))
    if populations[0]!=populations[1]:
        raise ValidationError('retained routed RX budget source roles differ')


def _import_routed_measurement(*, kind, output, identity, resolution, importer, export,
                               source_dispositions=None, **options):
    """Keep partial observations separate from complete timing databases."""
    measured = output/f"{kind}.tsv"
    observation = {"path":str(measured), "sha256":file_sha256(measured), "bytes":measured.stat().st_size}
    stage = {"status":resolution["status"], "resolution":resolution, "export":export,
        "measurement":observation}
    if resolution["status"] == "pass":
        destination = output/("boundary-timing.json" if kind == "boundary_timing" else f"{kind}.json")
        if source_dispositions is None:
            stage["import"] = importer(measured, identity, destination, **options)
        else:
            from .vivado_optimization import OptimizationProofView
            with OptimizationProofView(Path(source_dispositions["output"]),
                    expected_fpga=source_dispositions["fpga"]) as proof:
                stage["import"] = importer(measured, identity, destination,
                    optimization_proofs=proof, **options)
    else:
        stage["import"] = {"status":"incomplete", "reason":"unassociated-source-endpoints",
            "unresolved_queries":resolution["unresolved_query_rows"]}
    return stage


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
    word_phy_plan_path:Path|None=None,word_wiring_path:Path|None=None,word_schedule_path:Path|None=None,
    retain_routed_checkpoint:bool=False,
    route_after_proof_failure:bool=False,
    dut_timing_io:Mapping[str,Any]|None=None,
    routed_snapshot:Mapping[str,Any]|None=None,
) -> dict[str, Any]:
    from .vivado_backend import _resolve_vivado, _read_metrics, _integer, _number, vivado_runtime_xdc, write_vivado_boundary_timing_query
    from .vivado_physical_timing import import_indexed_vivado_boundary, import_vivado_local_timing
    from .logic_segment_timing import import_vivado_logic_segment_timing
    from .vivado_session import prepare_manifest
    from .benchmark import timing_io_sha256

    continuation = (synthesized_checkpoint,synthesized_checkpoint_sha256,continuation_source_report)
    word_mode=word_phy_plan_path is not None or word_wiring_path is not None
    if word_mode and (word_phy_plan_path is None or word_wiring_path is None or virtual_io_contract is not None):
        raise ValidationError('native word implementation requires its plan and wiring together, without a bit-adapter contract')
    native_word_mode=word_mode and word_schedule_path is not None
    if native_word_mode and boundary_identity_path is None:
        raise ValidationError('native word observations require canonical boundary identities')
    if not word_mode and word_schedule_path is not None:
        raise ValidationError('native word schedule requires its physical word plan')
    if not isinstance(retain_routed_checkpoint,bool):raise ValidationError('routed-checkpoint retention must be explicit Boolean')
    if not isinstance(route_after_proof_failure,bool) or (route_after_proof_failure and not retain_routed_checkpoint):
        raise ValidationError('proof-failure routing requires explicit retained snapshots')
    if native_word_mode:retain_routed_checkpoint=True
    if routed_snapshot is not None and (not native_word_mode or any(value is not None for value in continuation)):
        raise ValidationError('retained routed adoption requires native word mode without synthesized continuation')
    if routed_snapshot is not None and set(routed_snapshot)!={'directory','checkpoint_sha256','source_report','native_observation'}:
        raise ValidationError('retained routed adoption requires directory, checkpoint digest, mapped report and closed observation')
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
    for schema in ("boundary-timing-store-v3", "logic-segment-timing-store-v3", "local-path-timing-store-v3"):
        require_native_schema(schema)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any((output_dir/name).exists() for name in ("native-session.json","mapped_cells.tsv","vivado-implementation.log")):
        raise ValidationError("indexed Vivado implementation requires a fresh native output directory")
    executable = _resolve_vivado(executable) if routed_snapshot is None else None
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
    xdc.write_text(vivado_runtime_xdc(ir_path, runtime, dut_frequency_mhz, dut_timing_io), encoding="utf-8")
    prefix, adapter, wrapper, io_xdc, io_query = "", "", "", "", ""
    top_report = None
    word_sources=()
    word_source_preparation=None
    bank_placement_request=None
    if word_mode:
        from .vivado_word_top import write_vivado_word_top
        for path in (word_phy_plan_path,word_wiring_path):
            if not path.is_file()or path.stat().st_size>16*1024*1024:
                raise ValidationError('native word plan/wiring source is missing or exceeds its bounded board scope')
        word_sources=tuple(bind_store_source(path,'native-word-physical-source-v1')for path in (word_phy_plan_path,word_wiring_path))
        top_report=write_vivado_word_top(ir_path,output_dir,fpga=fpga,
            dut_clock_ports=tuple(port for port in runtime['timing_model']['dut_clock_ports'] if port in ports),
            board=read_json(word_wiring_path),plan=read_json(word_phy_plan_path))
        if top_report['part']!=part or implementation_timing['tdm_clock']['period_ns']!=5.0:
            raise ValidationError('native word part or TDM clock differs from its PHY model')
        write_json(output_dir/'native-word-preparation.json',top_report,compact=True)
        top,prefix=top_report['top'],top_report['hierarchy_prefix']
        adapter,wrapper=str(output_dir/'native-word-banks.sv'),str(output_dir/'physical_top.sv')
        io_xdc,io_query=str(output_dir/'native-word-fixed-io.xdc'),str(output_dir/'fixed-io-query.tsv')
        if native_word_mode:
            from .native_word_source_catalog import prepare_native_word_sources
            word_source_preparation=prepare_native_word_sources(fpga,word_schedule_path,boundary_identity_path,
                index_path,output_dir/'native-word-sources')
            if routed_snapshot is None:
                from .native_bank_placement import write_native_bank_placement_request
                write_native_bank_placement_request(fpga,word_phy_plan_path,output_dir/'native-word-sources',
                    output_dir/'native-bank-placement')
                bank_placement_request=output_dir/'native-bank-placement/request.json'
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
    if boundary_identity_path is not None and not native_word_mode:
        query = output_dir/"boundary-query.tsv"
        boundary_query_report = write_vivado_boundary_timing_query(ir_path,boundary_identity_path,query)
        queries["boundary_timing"] = query
    for kind,query in (("logic_segment_timing",logic_query_path),("local_path_timing",local_query_path)):
        if query is not None and not native_word_mode:
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
        dut_frequency_mhz=dut_frequency_mhz,word_phy_directory=output_dir if word_mode else None,
        retain_routed_checkpoint=retain_routed_checkpoint,route_after_proof_failure=route_after_proof_failure,
        source_hierarchy_prefix=prefix,word_source_directory=output_dir/'native-word-sources'if native_word_mode else None,
        bank_placement_request=bank_placement_request)
    threads = int(os.environ.get("EMUFLOW_VIVADO_THREADS", "2"))
    if threads < 1:
        raise ValidationError("Vivado thread count must be positive")
    input_files = {"physical_ir":ir_path,"mapped_index":index_path,"source_functions":functions_path,
                   "mapped_verilog":mapped_verilog_path,**{f"query_{kind}":path for kind,path in queries.items()}}
    if word_mode:input_files.update(word_phy_plan=word_phy_plan_path,word_wiring=word_wiring_path)
    if native_word_mode:input_files.update(word_schedule=word_schedule_path,word_boundary_identity=boundary_identity_path)
    input_identity = {key:{"path":str(path.resolve()),"sha256":file_sha256(path)} for key,path in input_files.items()}
    input_identity["runtime_sha256"] = hashlib.sha256(json.dumps(runtime,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    for key,expected_sha in (("mapped_index",mapped_report["mapped_index"]["sha256"]),
                              ("source_functions",mapped_report["source_functions"]["sha256"]),
                              ("mapped_verilog",mapped_report["output_sha256"])):
        if input_identity[key]["sha256"] != expected_sha:
            raise ValidationError("Vivado mapped source identity differs before native execution")
    routed_checkpoint=output_dir/'routed.dcp'
    optimized_checkpoint=output_dir/'optimized.dcp'
    inventory_root=output_dir
    retained_parents=[]
    retained_artifacts={}
    if routed_snapshot is None:
        implementation = execute_vivado(executable, _vivado_script("implement_indexed_partition.tcl"),
            [part, str(mapped_verilog_path.resolve()), top, str(xdc), str(output_dir), str(expected), prefix,
             adapter, wrapper, io_xdc, io_query, place_directive, route_directive, str(dut_count), str(threads), str(configuration)],
            output_dir, "vivado-implementation.log")
    else:
        import shutil
        retained=Path(routed_snapshot['directory']).resolve()
        if retained==output_dir:raise ValidationError('retained routed adoption requires a fresh output directory')
        routed_checkpoint=retained/'routed.dcp';optimized_checkpoint=retained/'optimized.dcp'
        inventory_root=retained
        continuation_validation=_validate_snapshot_inputs(routed_checkpoint,routed_snapshot['checkpoint_sha256'],
            Path(routed_snapshot['source_report']),mapped_report,mapped_verilog_path,output_dir,stage='routed')
        old_session=retained/'native-session.json'
        prior=read_json(old_session);current=read_json(output_dir/'native-session.json')
        for key in ('schema','fpga','part','mapped_index_sha256','source_hierarchy_prefix','implementation_timing','synthesis'):
            if prior.get(key)!=current.get(key):
                raise ValidationError('retained routed session differs: '+key)
        if file_sha256(retained/'clock-contract.tsv')!=file_sha256(output_dir/'clock-contract.tsv'):
            raise ValidationError('retained routed clock contract differs')
        copied=('implementation_metrics.tsv','timing_summary.rpt','route_status.rpt','drc.rpt',
            'utilization.rpt','word-data-budget-metrics.tsv')
        inventories=('mapped_cells.tsv','optimized_mapped_cells.tsv','routed_mapped_cells.tsv')
        for name in copied+inventories+('native-session.json','vivado-implementation.log','word-data-source-origins.tsv'):
            path=retained/name
            if not path.is_file()or path.stat().st_size==0:
                raise ValidationError('retained routed implementation artifact is missing: '+name)
            retained_parents.append(bind_store_source(path,'physical-input-document-v1'))
        _require_same_word_data_origins(retained/'word-data-source-origins.tsv',output_dir/'word-data-source-origins.tsv')
        retained_parents.append(bind_store_source(routed_checkpoint,'physical-input-document-v1'))
        for name in copied:shutil.copyfile(retained/name,output_dir/name)
        require_store_sources_unchanged(retained_parents)
        for name in ('native-session.json','vivado-implementation.log'):
            path=retained/name
            retained_artifacts['retained/'+name]=dict(path=str(path),sha256=file_sha256(path),bytes=path.stat().st_size)
        current['synthesis']['mode']='retained-routed'
        current['execution_mode']='retained-routed-analysis-only'
        if prior.get('native_bank_placement') is not None:
            current['native_bank_placement']=prior['native_bank_placement']
        current['retained_snapshot']=dict(directory=str(retained),checkpoint_sha256=routed_snapshot['checkpoint_sha256'],
            source_session=retained_artifacts['retained/native-session.json'])
        write_json(output_dir/'native-session.json',current,compact=True)
        implementation=dict(status='reused',invoked_tool=False,log=str(retained/'vivado-implementation.log'),
            source_session=str(old_session),scope='completed routed snapshot; original log may include later legacy export failures')
    metrics = _read_metrics(output_dir / "implementation_metrics.tsv")
    if metrics.get("part") != part or _integer(metrics, "dut_clock_count") != dut_count:
        raise ValidationError("Vivado part/clock identity differs from Phase6")
    native_observation=None
    if native_word_mode:
        from .vivado_lineage import import_cell_lineage
        from .native_word_observation import observe_native_word_checkpoint,reuse_native_word_observation
        coverage=import_cell_lineage(index_path,functions_path,
            {stage:inventory_root/name for stage,name in [('synthesized','mapped_cells.tsv'),
                ('optimized','optimized_mapped_cells.tsv'),('routed','routed_mapped_cells.tsv')]},
            output_dir/'cell-lineage.sqlite',mapped_sha256=mapped_report['mapped_index']['sha256'])
        if routed_snapshot is None:
            native_observation=observe_native_word_checkpoint(fpga,routed_checkpoint,
                output_dir/'native-word-sources',output_dir/'native-word-boundaries.tsv',
                output_dir/'native-word-observation',executable=executable,threads=threads,runtime_constraints=xdc)
        else:
            native_observation=reuse_native_word_observation(fpga,routed_checkpoint,
                output_dir/'native-word-sources',output_dir/'native-word-boundaries.tsv',
                Path(routed_snapshot['native_observation']),output_dir/'native-word-observation')
        preparation={'status':'pass','coverage':coverage,'timing_coverage':{
            'status':'native-observations-complete','policy':'native-clock-and-interface-v1',
            'original_member_accounting':'pending-independent-phase7c',**native_observation['counts']}}
    else:
        preparation = read_json(output_dir/"routed-query-preparation.json")
    if preparation.get("status") != "pass":
        raise ValidationError("native routed query preparation did not pass")
    coverage = preparation["coverage"]
    timing_coverage = preparation["timing_coverage"]
    lineage_artifact, lineage_binding = _retained_lineage_artifact(output_dir, coverage)
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
    source_dispositions = preparation.get("optimization_proofs")
    query_mode = "clock-aware-reference" if word_mode else "datapath-only"
    cost_expectation = {"expected_sampling_cost_policy": SAMPLING_COST_POLICY} if word_mode else {}
    export = {"status":"pass","mode":"current-routed-native-session","log":implementation["log"],
              "timing_query_mode":query_mode}
    if boundary_identity_path is not None and not native_word_mode:
        stages["boundary_timing"] = _import_routed_measurement(kind="boundary_timing", output=output_dir,
            identity=boundary_identity_path, resolution=preparation["resolutions"]["boundary_timing"],
            importer=import_indexed_vivado_boundary, export=export,
            source_dispositions=source_dispositions,
            measurement_scope="native-word-registered-interface" if word_mode else
                ("board-integrated-interface" if virtual_io_contract is not None else "partition-boundary"),
            **cost_expectation)
        stages["boundary_timing"]["query"] = boundary_query_report
    for kind, identity, query, importer in (
        ("logic_segment_timing", logic_identity_path, logic_query_path, import_vivado_logic_segment_timing),
        ("local_path_timing", local_identity_path, local_query_path, import_vivado_local_timing),
    ):
        if (identity is None) != (query is None):
            raise ValidationError(f"Vivado {kind} requires identity and query together")
        if identity is None or native_word_mode:
            continue
        stages[kind] = _import_routed_measurement(kind=kind, output=output_dir, identity=identity,
            resolution=preparation["resolutions"][kind], importer=importer, export=export,
            source_dispositions=source_dispositions, **cost_expectation)
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
        if native_word_mode:
            checks=native_observation['physical_checks']['native-control-checks.tsv']
            values={row['delay_type']:float(row['slack_ns']) for row in checks if row['measurement']=='native-path'}
            word_timing={'status':'observed','scope':WORD_CONTROL_TIMING_SCOPE,'checks':len(values),
                'wns_ns':values.get('max'),'whs_ns':values.get('min'),
                'rx_scope_complete':native_observation['counts']['missing_rx_origins']==0,
                'missing_rx_origins':native_observation['counts']['missing_rx_origins'],
                'fixed_clock_contract_met':native_observation['fixed_clock_control_timing_met']}
        else:
            word_timing=_word_infrastructure_timing(metrics,active_banks=top_report['active_banks'])
        require_store_sources_unchanged(word_sources)
    require_store_sources_unchanged((lineage_binding,))
    artifacts = {"cell-lineage.sqlite":lineage_artifact,**retained_artifacts}
    if source_dispositions is not None:
        path = Path(source_dispositions["output"])
        artifacts[path.name] = {"path":str(path), "sha256":file_sha256(path), "bytes":path.stat().st_size}
    common_artifacts=("route_status.rpt", "drc.rpt", "timing_summary.rpt", "utilization.rpt", "implementation_metrics.tsv",
                 "native-session.json", "native-session.tcl", "clock-contract.tsv")
    for name in common_artifacts+(()if native_word_mode else("routed-query-preparation.json", "timing-exports.tsv")):
        path = output_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError(f"Vivado physical artifact is missing: {name}")
        artifacts[name] = {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
    if word_mode:
        names=('native-word-preparation.json','word-data-source-origins.tsv','word-data-budget-metrics.tsv')
        if not native_word_mode:names+=('native-word-clock-census.tsv','native-word-rx-sources.tsv')
        for name in names:
            path=output_dir/name
            artifacts[name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
        if native_word_mode:
            for name,artifact in native_observation['files'].items():
                artifacts['native-word-observation/'+name]=artifact
            for name in ('native-word-sources/source-catalog.json','native-word-observation/native-observation.json'):
                path=output_dir/name
                artifacts[name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
    placement=read_json(output_dir/'native-session.json').get('native_bank_placement')
    if placement is not None:
        from .native_bank_placement import read_native_bank_placement_request
        request=Path(placement['request']['path'])
        observed,bindings=read_native_bank_placement_request(request,fpga)
        retained_parents.extend(bindings)
        if file_sha256(request)!=placement['request']['sha256']:
            raise ValidationError('native bank placement request changed during implementation')
        placement_root=retained if routed_snapshot is not None else output_dir
        placement_files={'native-bank-placement/request.json':request,
                         **{'native-bank-placement/'+name:Path(reference['path'])for name,reference in observed['files'].items()},
                         **{name:placement_root/name for name in ('bank-affinity.tsv','bank-placement-metrics.tsv')}}
        for name in ('bank-tx-cones.tsv','bank-replication-nets.tsv','bank-replication-metrics.tsv'):
            if (placement_root/name).is_file():placement_files[name]=placement_root/name
        for name,path in placement_files.items():
            retained_parents.append(bind_store_source(path,'physical-input-document-v1'))
            artifacts[name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
    if retain_routed_checkpoint:
        path=routed_checkpoint
        if not path.is_file():raise ValidationError('requested routed debug checkpoint is missing')
        artifacts[path.name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
    policy_root=(retained if routed_snapshot is not None else output_dir)/'implementation-policy'
    if native_word_mode and policy_root.is_dir():
        for path in sorted(policy_root.iterdir()):
            if path.is_file()and path.suffix in ('.tsv','.rpt','.txt'):
                retained_parents.append(bind_store_source(path,'physical-input-document-v1'))
                artifacts['implementation-policy/'+path.name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
    if route_after_proof_failure:
        path=optimized_checkpoint
        if not path.is_file():raise ValidationError('requested optimized debug snapshot is missing')
        artifacts[path.name]={'path':str(path),'sha256':file_sha256(path),'bytes':path.stat().st_size}
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
            "clock_domain_presence": {"fabric":True,"dut":dut_count>0,"cross":dut_count>0},
            **{name: _number(metrics, name) for name in ("dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns", "critical_path_ns")},
            "clock_domain_delays_ns": {domain: _number(metrics, name) for domain, name in (("overall", "critical_path_ns"), ("dut", "dut_delay_ns"), ("fabric", "fabric_delay_ns"), ("cross", "fabric_to_dut_delay_ns"))}},
        "resource_usage": usage, "hard_resources": counts, "artifacts": artifacts}
    require_store_sources_unchanged(retained_parents)
    if routed_snapshot is not None and file_sha256(routed_checkpoint)!=routed_snapshot['checkpoint_sha256']:
        raise ValidationError('retained routed snapshot changed during finalization')
    validation = validate_physical_partition_result(result, backend="vivado", fpga=fpga, part=part, original_cells=original_cells, transport_cells=transport_cells)
    native_scripts=("native_session.tcl","source_inventory.tcl","resource_inventory.tcl","timing_contract.tcl",
        "export_boundary_timing.tcl","export_logic_segment_timing.tcl")
    if word_mode:native_scripts+=("word_native_physical.tcl","word_data_budgets.tcl","selectio_ip.tcl")
    if placement is not None:native_scripts+=("native_bank_placement.tcl","native_word_timing.tcl","word_phase_context.tcl")
    if native_word_mode:native_scripts+=("native_implementation_policy.tcl",)
    report = {"schema": "emuflow.vivado-partition-report/v1", "status": "pass", "provider": ("indexed-phase6-vivado-word-physical-v5"if native_word_mode else "indexed-phase6-vivado-word-physical-v4")if word_mode else "indexed-phase6-vivado-physical-v2",
        "tool": {"executable": executable, "version": metrics["vivado_version"], "threads": threads,
            "invoked_for_implementation":routed_snapshot is None},
        "implementation": implementation, "continuation": continuation_validation, "input_identity": input_identity,
        "implementation_timing": implementation_timing,
        "implementation_options": {"place_directive": place_directive, "route_directive": route_directive,
                                   "dut_frequency_mhz": dut_frequency_mhz,"retain_routed_checkpoint":retain_routed_checkpoint,
                                   "route_after_proof_failure":route_after_proof_failure,
                                   **({'execution_policy':'bounded-native-word-physical-v1'}
                                      if native_word_mode and (policy_root/'summary.tsv').is_file()else{}),
                                   **({'dut_timing_io_sha256':timing_io_sha256(dut_timing_io)}if dut_timing_io is not None else {})},
        "native_session": {"mode":"routed-snapshot-native-observations-v1"if native_word_mode else"current-design-stage-exports-v2","timing_query_mode":query_mode,
            "scripts_sha256": {name:file_sha256(_vivado_script(name)) for name in native_scripts},
            "planner_sha256":file_sha256(Path(__file__).with_name("vivado_session.py"))},
        "cell_coverage": coverage, "timing_coverage": timing_coverage, **stages,
        **({'native_word_observation':native_observation,'native_word_sources':word_source_preparation}if native_word_mode else{}),
        **({'native_bank_placement':placement}if placement is not None else{}),
        **({"optimization_proofs":source_dispositions} if source_dispositions is not None else {}),
        **({"virtual_io_implementation": virtual_io} if virtual_io is not None else {}),
        **({'word_phy_implementation':{'status':'pass','preparation':top_report,
            'sampling_cost_policy':SAMPLING_COST_POLICY,
            'infrastructure_timing':word_timing,
            'differential_pairs':pairs,'serial_pad_timing_scope':'fixed-link-model-external-to-local-STA; no board/SI signoff'}}if word_mode else{}),
        "result": result, "validation": validation,
        "scope":"physical-implementation; whole-design timing requires complete timing coverage"}
    write_json(output_dir / "vivado-partition-report.json", report, compact=True)
    return report
