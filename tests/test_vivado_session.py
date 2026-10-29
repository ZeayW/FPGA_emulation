import csv
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json, write_json
from emuflow.vivado_session import (
    SCHEMA, prepare_frontiers, confirm_frontiers, prepare_timing, prepare_checked_timing, prepare_diagnostic_timing, prepare_manifest, _tcl_word,
)
from emuflow.vivado_optimization import OptimizationProofView
from emuflow.vivado_physical import validate_synthesized_continuation
from tests.test_vivado_constant_frontier import conditional_model, set_native_constant

ROOT = Path(__file__).resolve().parents[1]

def _tcl86():
    for candidate in (shutil.which("tclsh8.6"),shutil.which("tclsh9.0"),shutil.which("tclsh")):
        if candidate:
            version = subprocess.run([candidate],input="puts [info patchlevel]\n",text=True,capture_output=True,check=True).stdout.strip()
            if tuple(int(v) for v in version.split('.')[:2]) >= (8,6):
                return candidate
    return None

TCL86 = _tcl86()


@pytest.mark.skipif(TCL86 is None, reason="Tcl 8.6 required")
@pytest.mark.parametrize("constant", ["GND", "VCC"])
@pytest.mark.parametrize('kind',['output-port','module-output'])
def test_native_output_frontier_exports_the_exact_port_bit_and_driver(tmp_path, constant, kind):
    query, observed = tmp_path/"query.tsv", tmp_path/"observed.tsv"
    name = "mapped_partition/q[1]" if kind=='module-output' else "q[1]"
    query.write_text("source_origin\tnative_cell_hex\tobject_kind\n" +
        f"s0\t{name.encode().hex()}\t{kind}\n")
    program = f"set mock_constant {constant}\nset mock_port {_tcl_word(name)}\n" + r'''
proc current_design {args} {return current}
proc get_ports {args} {
  if {[lindex $args end] eq "DIRECTION == IN"} {return {}}
  return port
}
proc get_nets {args} {return {net alias_net}}
proc get_pins {args} {
  if {[lsearch -exact $args -hier]>=0} {return port}
  return {driver driver}
}
proc get_cells {args} {return constant}
proc get_property {args} {
  global mock_constant mock_port
  lassign [lrange $args end-1 end] key object
  switch -- $key {
    NAME {return [expr {$object eq "port" ? $mock_port : "constant/OUT"}]}
    DIRECTION {return OUT}
    REF_NAME {return $mock_constant}
    REF_PIN_NAME {return [expr {$mock_constant eq "GND" ? "G" : "P"}]}
    default {return ""}
  }
}
set ::emuflow_native_export_stage optimized
set argc 3
'''
    program += "set argv [list @current-design " + _tcl_word(query) + " " + _tcl_word(observed) + "]\n"
    program += "if {[catch {source " + _tcl_word(ROOT/"scripts/vivado/inspect_constant_frontiers.tcl") + "} message]} {puts stderr $message;exit 2}\n"
    result = subprocess.run([TCL86], input=program, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    with observed.open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["native_cell_hex"] == name.encode().hex()
    assert rows[0]["ref"] == ('MODULE_OUTPUT'if kind=='module-output'else'OUTPUT_PORT') and rows[0]["pin"] == "PORT"
    assert rows[0]["driver_kind"] == ("zero" if constant == "GND" else "one")
    assert rows[0]["driver_ref"] == constant


@pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')
def test_native_carry_frontier_exports_its_complete_input_vector_and_mode(tmp_path):
    query,observed=tmp_path/'query.tsv',tmp_path/'observed.tsv'
    query.write_text('source_origin\tnative_cell_hex\tobject_kind\ns0\t'+b'p/front'.hex()+'\tcell\n')
    program=r'''
proc current_design {args} {return current}
proc get_ports {args} {return {}}
proc get_nets {args} {return net}
proc get_pins {args} {
  if {[lsearch -exact $args -leaf]>=0} {return driver}
  return pin
}
proc get_cells {args} {
  if {[lsearch -exact $args -hier]>=0} {return front}
  return constant
}
proc get_property {args} {
  lassign [lrange $args end-1 end] key object
  switch -- $key {
    EMUFLOW_SOURCE {return s0}
    REF_NAME {return [expr {$object eq "front" ? "CARRY8" : "GND"}]}
    CARRY_TYPE {return SINGLE_CY8}
    REF_PIN_NAME {return G}
    NAME {return constant/G}
    default {return ""}
  }
}
set ::emuflow_native_export_stage optimized
set argc 3
'''
    program+='set argv [list @current-design '+_tcl_word(query)+' '+_tcl_word(observed)+']\n'
    program+='if {[catch {source '+_tcl_word(ROOT/'scripts/vivado/inspect_constant_frontiers.tcl')+'} message]} {puts stderr $message;exit 2}\n'
    result=subprocess.run([TCL86],input=program,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    with observed.open()as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==18
    assert {row['pin']for row in rows}=={'CI','CI_TOP',*(f'{bus}[{bit}]'for bus in ('DI','S')for bit in range(8))}
    assert all(row['ref']=='CARRY8'and row['carry_type']=='SINGLE_CY8'and row['driver_kind']=='zero'for row in rows)



def session_fixture(tmp_path, *, require_constant=True, dependent=False):
    mapped, functions, _, inventory, facts, digest, origins = conditional_model(tmp_path, dependent=dependent)
    out = tmp_path/"native";out.mkdir()
    (out/"synthesized.dcp").write_bytes(b"checked synthetic native snapshot")
    shutil.copyfile(inventory, out/"mapped_cells.tsv")
    with inventory.open() as source, (out/"routed_mapped_cells.tsv").open("w") as target:
        rows = csv.reader(source, delimiter="\t")
        writer = csv.writer(target, delimiter="\t", lineterminator="\n")
        writer.writerow(next(rows))
        for index, row in enumerate(rows):
            writer.writerow(row[:4]+[f"SLICE_X{index}Y0", "AFF", str(index), "0", "LEAF"])
    query = tmp_path/"queries.tsv"
    end = "conditional/D" if require_constant else "front/I1"
    query.write_text("endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex\n"+
        "\t".join(["path0".encode().hex(), "local", "pin", "kept/Q".encode().hex(), "pin", end.encode().hex(), "", ""])+"\n")
    session = dict(schema=SCHEMA, source_preflight_stage="synthesized", timing_query_mode="datapath-only", fpga="fpga0", part="xcvu19p-fsva3824-2-e", output=str(out),
        mapped_index=str(mapped), functions=str(functions), mapped_index_sha256=digest,
        synthesized_checkpoint=str(out/"synthesized.dcp"), synthesized_checkpoint_sha256=file_sha256(out/"synthesized.dcp"),
        queries={"local_path_timing": str(query)})
    return session, facts, origins


@pytest.mark.parametrize("dependent", [False, True])
def test_source_candidates_native_facts_and_routed_proof_are_one_checked_pipeline(tmp_path, dependent):
    session, facts, origins = session_fixture(tmp_path, dependent=dependent)
    out = Path(session["output"])
    result = prepare_frontiers(session)
    assert result["constant_sources"] == (2 if dependent else 1)
    assert result["native_frontiers"] == 1
    assert (out/"synthesized-lineage.sqlite").is_file()
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    assert confirm_frontiers(session)["confirmed_sources"] == result["constant_sources"]
    assert prepare_checked_timing(session)["query_classes"] == 1
    preparation = read_json(out/"routed-query-preparation.json")
    owner = Path(preparation["coverage"]["output"])
    assert owner.is_file() and file_sha256(owner) == preparation["coverage"]["sha256"]
    with sqlite3.connect(owner) as placement:
        assert placement.execute("SELECT COUNT(*) FROM objects o JOIN sites s ON s.seq=o.site_seq WHERE o.stage='routed'").fetchone()[0] > 0
    assert not (out/"mapped_cells.tsv").exists()
    assert not (out/"routed_mapped_cells.tsv").exists()
    assert not (out/"synthesized-lineage.sqlite").exists()
    with OptimizationProofView(out/"optimization-proofs.sqlite", expected_fpga="fpga0") as proof:
        assert proof.require_eliminated_source(f"s{origins['conditional']}")["initialization"] == "vendor-confirmed"
    assert f"optimized-away:s{origins['conditional']}" in (out/"local_path_timing-resolved.tsv").read_text()
    rows = list(csv.DictReader((out/"timing-exports.tsv").open(), delimiter="\t"))
    assert len(rows) == 1
    assert bytes.fromhex(rows[0]["output_hex"]).decode() == str(out/"local_path_timing.tsv")


def test_unrequired_conditional_states_remain_unproved(tmp_path):
    session, facts, origins = session_fixture(tmp_path, require_constant=False)
    out = Path(session["output"])
    assert prepare_frontiers(session)["constant_sources"] == 0
    assert confirm_frontiers(session)["confirmed_sources"] == 0
    prepare_checked_timing(session)
    with OptimizationProofView(out/"optimization-proofs.sqlite", expected_fpga="fpga0") as proof:
        with pytest.raises(ValidationError):
            proof.require_eliminated_source(f"s{origins['conditional']}")
    assert "optimized-away" not in (out/"local_path_timing-resolved.tsv").read_text()


@pytest.mark.parametrize("diagnostic_mode", [False, True])
def test_routed_queries_preserve_missing_endpoints_without_inventing_removal(tmp_path, diagnostic_mode):
    session, _, _ = session_fixture(tmp_path)
    out = Path(session["output"])
    source = Path(session["queries"]["local_path_timing"])
    with source.open("a") as stream:
        stream.write("\t".join([b"kept-path".hex(), "local", "pin", b"kept/Q".hex(),
            "pin", b"front/I1".hex(), "", ""])+"\n")
    parents = {path: file_sha256(path) for path in
        (source, out/"mapped_cells.tsv", out/"routed_mapped_cells.tsv", out/"synthesized.dcp")}
    operation = prepare_diagnostic_timing if diagnostic_mode else prepare_timing
    assert operation(session)["status"] == ("diagnostic" if diagnostic_mode else "pass")
    diagnostic = out/"timing-diagnostic" if diagnostic_mode else out
    preparation = read_json(diagnostic/("preparation.json" if diagnostic_mode else "routed-query-preparation.json"))
    if diagnostic_mode:
        assert preparation["qualified_physical_result"] is False
    else:
        assert preparation["timing_coverage"] == {"status":"incomplete", "requested_queries":2,
            "measurable_queries":1, "unresolved_queries":1}
    counts = preparation["resolutions"]["local_path_timing"]
    assert (counts["queries"], counts["measurable_queries"], counts["unresolved_query_rows"]) == (2, 1, 1)
    assert Path(counts["output"]).is_file()
    with (diagnostic/"local_path_timing-resolved.tsv").open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 1 and bytes.fromhex(rows[0]["endpoint_hex"]).decode() == "kept-path"
    missing = (diagnostic/"local_path_timing-unresolved.tsv").read_text()
    assert "unresolved-source" in missing and "optimized-away" not in missing
    with sqlite3.connect(diagnostic/"local_path_timing-routed-unassociated.sqlite") as db:
        assert db.execute("SELECT role FROM unresolved_sources").fetchall() == [("end",)]
    assert (out/"timing-exports.tsv").exists() is (not diagnostic_mode)
    assert not (out/"optimization-proofs.sqlite").exists()
    assert (out/"routed-query-preparation.json").exists() is (not diagnostic_mode)
    assert parents == {path: file_sha256(path) for path in parents}
    if diagnostic_mode:
        with pytest.raises(FileExistsError):
            prepare_diagnostic_timing(session)


def test_lut_reduction_does_not_reject_routed_export_or_imply_elimination(tmp_path):
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    out = Path(session["output"])
    inventory = out/"routed_mapped_cells.tsv"
    inventory.write_text(inventory.read_text().replace("p/front\tLUT2\t", "p/front\tLUT1\t"))
    query = Path(session["queries"]["local_path_timing"])
    with query.open("a") as stream:
        stream.write("\t".join([b"retained".hex(), "local", "pin", b"kept/Q".hex(),
            "pin", b"kept/D".hex(), "", ""])+"\n")
    result = prepare_timing(session)
    assert result["timing_coverage"]["measurable_queries"] == 1
    assert result["timing_coverage"]["unresolved_queries"] == 1
    preparation = read_json(out/"routed-query-preparation.json")
    assert preparation["coverage"]["stages"]["routed"]["changed_type_objects"] == 1
    assert "optimized-away" not in (out/"local_path_timing-unresolved.tsv").read_text()
    assert not (out/"constant-frontier-plan.json").exists()
    assert not (out/"optimization-proofs.sqlite").exists()


@pytest.mark.parametrize("complete", [False, True])
def test_physical_measurements_survive_incomplete_source_coverage(tmp_path, complete):
    from emuflow.vivado_physical import _import_routed_measurement
    from unittest.mock import Mock
    measured = tmp_path/"local_path_timing.tsv"
    measured.write_text("actual vendor measurements\n")
    importer = Mock(return_value={"status":"pass"})
    result = _import_routed_measurement(kind="local_path_timing", output=tmp_path,
        identity=tmp_path/"identity.sqlite", resolution={"status":"pass" if complete else "incomplete",
            "unresolved_query_rows":0 if complete else 1}, importer=importer, export={"status":"pass"})
    assert measured.is_file() and result["measurement"]["sha256"] == file_sha256(measured)
    assert result["status"] == ("pass" if complete else "incomplete")
    assert importer.call_count == int(complete)
    if complete:
        assert "optimization_proofs" not in importer.call_args.kwargs
    else:
        assert result["import"] == {"status":"incomplete", "reason":"unassociated-source-endpoints", "unresolved_queries":1}
        assert not (tmp_path/"local_path_timing.json").exists()


def test_optional_source_proof_rejects_unassociated_nonconstant_endpoint(tmp_path):
    session, _, _ = session_fixture(tmp_path)
    inventory = Path(session["output"])/"mapped_cells.tsv"
    inventory.write_text("\n".join(line for line in inventory.read_text().splitlines() if not line.startswith("p/kept\t"))+"\n")
    with pytest.raises(ValidationError, match="no supported native association"):
        prepare_frontiers(session)


def test_changed_checkpoint_or_native_driver_cannot_promote_constants(tmp_path):
    session, facts, _ = session_fixture(tmp_path)
    out = Path(session["output"])
    prepare_frontiers(session)
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    checkpoint = Path(session["synthesized_checkpoint"])
    checkpoint.write_bytes(b"a different checkpoint")
    with pytest.raises(ValidationError, match="different synthesized checkpoint"):
        confirm_frontiers(session)
    checkpoint.write_bytes(b"checked synthetic native snapshot")
    shutil.copyfile(facts,out/'constant-frontiers.tsv')
    set_native_constant(out/'constant-frontiers.tsv','I0',1)
    with pytest.raises(ValidationError, match="pin bindings"):
        prepare_checked_timing(session)
    assert not (out/"optimization-proofs.sqlite").exists()


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_current_snapshot_exports_require_native_stage_and_do_not_reopen_checkpoint(tmp_path):
    helper = ROOT/"scripts/vivado/timing_contract.tcl"
    source = f"source {_tcl_word(helper)}\n"+r'''
proc current_design {args} {return current}
proc open_checkpoint {path} {global opened;set opened $path}
if {![catch {emuflow_open_export_design @current-design routed}]} {error "unchecked snapshot accepted"}
set ::emuflow_native_export_stage synthesized
if {![catch {emuflow_open_export_design @current-design routed}]} {error "wrong stage accepted"}
emuflow_open_export_design @current-design synthesized
if {[info exists opened]} {error "current snapshot reopened a checkpoint"}
set ::emuflow_native_export_stage routed
emuflow_open_export_design @current-design routed
emuflow_open_export_design explicit.dcp routed
if {![string match */explicit.dcp $opened]} {error "legacy explicit checkpoint was not opened"}
puts pass
'''
    result = subprocess.run([shutil.which("tclsh")], input=source, text=True, capture_output=True, check=True)
    assert not result.stderr
    assert result.stdout.strip() == "pass"


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_native_child_contract_survives_tcl_characters_and_isolates_vendor_python(tmp_path):
    out = tmp_path/'native $path[0] "quoted"';out.mkdir()
    runtime = {"fabric_clock": {"period_ns": 4}, "virtual_dut_clocks": {"clk": {"target_period_ns": 20, "nominal_period_ns": 200}},
               "timing_model": {"fabric_clock_port": "fabric_clk", "dut_clock_ports": ["clk"]}}
    config = prepare_manifest(out, fpga="fpga0", part="part", mapped_index=tmp_path/"mapped.sqlite", functions=tmp_path/"functions.sqlite",
        mapped_sha256="a"*64, queries={}, runtime=runtime, ports={"clk", "fabric_clk"})
    code = f"source {_tcl_word(config)}\nputs [lindex $emuflow_child_command 0]\nputs $emuflow_session_out\nputs $emuflow_start_checkpoint\n"
    result = subprocess.run([shutil.which("tclsh")], input=code, text=True, capture_output=True, check=True)
    assert not result.stderr
    assert result.stdout.splitlines() == ["/usr/bin/env", str(out), ""]


def test_continuation_rejects_changed_runtime_source_or_checkpoint(tmp_path):
    mapped, functions, _, _, _, digest, _ = conditional_model(tmp_path)
    old = tmp_path/"old";old.mkdir();new = tmp_path/"new";new.mkdir()
    checkpoint = old/"synthesized.dcp";checkpoint.write_bytes(b"snapshot")
    for path in (old/"runtime.xdc", new/"runtime.xdc"):
        path.write_text("original runtime")
    verilog = tmp_path/"dut.v"
    report = dict(schema="emuflow.vivado-netlist-report/v1", status="pass", provider="test", optimization_policy="policy", top="dut", emitted_instances=3,
        output_sha256=file_sha256(verilog), mapped_index=dict(path=str(mapped), sha256=file_sha256(mapped)),
        source_functions=dict(path=str(functions), sha256=file_sha256(functions)))
    source_report = tmp_path/"source-report.json";source_report.write_text(json.dumps(report))
    def validate():
        return validate_synthesized_continuation(checkpoint,file_sha256(checkpoint),source_report,report,verilog,new)
    assert validate()["status"] == "pass"
    (new/"runtime.xdc").write_text("different runtime")
    with pytest.raises(ValidationError, match="runtime/fixed-I/O"):
        validate()
    (new/"runtime.xdc").write_text("original runtime")
    changed = {**report, "top": "other"};source_report.write_text(json.dumps(changed))
    with pytest.raises(ValidationError, match="source report"):
        validate()
    source_report.write_text(json.dumps(report))
    with pytest.raises(ValidationError, match="checkpoint digest"):
        validate_synthesized_continuation(checkpoint,"0"*64,source_report,report,verilog,new)
    from emuflow.vivado_word_top import SCHEMA as WORD_TOP_SCHEMA,WORD_TOP_FILES
    for root in (old,new):
        artifacts={}
        for name in (*WORD_TOP_FILES,'fixed-io-timing-map.tsv','bank19_tx.tsv'):
            (root/name).write_text('frozen word input '+name)
            if name!='fixed-io-timing-map.tsv':artifacts[name]={'sha256':file_sha256(root/name)}
        write_json(root/'native-word-preparation.json',{'schema':WORD_TOP_SCHEMA,'status':'pass',
            'fpga':'fpga0','part':'xcpart','top':'dut','native_phy':{'rx_alignment':'word-one-stage-v1'},
            'source_board_sha256':'b'*64,'source_word_plan_sha256':'c'*64,'external_services':{},'artifacts':artifacts})
    assert 'bank19_tx.tsv'in validate()['input_contracts']
    (new/'bank19_tx.tsv').write_text('another PHY configuration')
    with pytest.raises(ValidationError,match='native word continuation input file differs'):
        validate()
    changed=read_json(new/'native-word-preparation.json')
    changed['artifacts']['bank19_tx.tsv']['sha256']=file_sha256(new/'bank19_tx.tsv')
    write_json(new/'native-word-preparation.json',changed)
    with pytest.raises(ValidationError,match='native word continuation input seal differs'):
        validate()


def test_routed_debug_checkpoint_is_explicit_and_keeps_the_native_control_contract(tmp_path):
    runtime={'fabric_clock':{'period_ns':5},'virtual_dut_clocks':{'clk':{'target_period_ns':20,'nominal_period_ns':200}},
        'timing_model':{'fabric_clock_port':'fabric_clk','dut_clock_ports':['clk']}}
    args=dict(fpga='fpga0',part='xcpart',mapped_index=tmp_path/'mapped.sqlite',functions=tmp_path/'functions.sqlite',
        mapped_sha256='a'*64,queries={},runtime=runtime,ports={'clk','fabric_clk'})
    config=prepare_manifest(tmp_path,**args,retain_routed_checkpoint=True)
    assert read_json(tmp_path/'native-session.json')['retain_routed_checkpoint']is True
    assert 'set emuflow_retain_routed_checkpoint "true"'in config.read_text()
    with pytest.raises(ValidationError,match='retention must be Boolean'):
        prepare_manifest(tmp_path,**args,retain_routed_checkpoint=1)


@pytest.mark.parametrize('retain,defer', [(False,True),(True,1),(True,'true')])
def test_proof_failure_routing_cannot_be_enabled_without_explicit_retained_snapshots(tmp_path,retain,defer):
    runtime={'fabric_clock':{'period_ns':5},'virtual_dut_clocks':{'clk':{'target_period_ns':20,'nominal_period_ns':200}},
        'timing_model':{'fabric_clock_port':'fabric_clk','dut_clock_ports':['clk']}}
    with pytest.raises(ValidationError,match='routing after a proof failure'):
        prepare_manifest(tmp_path,fpga='fpga0',part='xcpart',mapped_index=tmp_path/'mapped.sqlite',
            functions=tmp_path/'functions.sqlite',mapped_sha256='a'*64,queries={},runtime=runtime,
            ports={'clk','fabric_clk'},retain_routed_checkpoint=retain,route_after_proof_failure=defer)


def test_native_word_session_selects_sampling_costs_and_rejects_downgrades(tmp_path):
    from emuflow.vivado_session import load_session
    table=tmp_path/'native-word-clocks.tsv'
    table.write_text('clock_hex\tport_hex\tperiod_ns\n')
    runtime={'fabric_clock':{'period_ns':5},'virtual_dut_clocks':{'clk':{'target_period_ns':100}},
             'timing_model':{'fabric_clock_port':'fabric_clk','dut_clock_ports':['clk']}}
    config=prepare_manifest(tmp_path,fpga='fpga0',part='part',mapped_index=tmp_path/'mapped',
        functions=tmp_path/'functions',mapped_sha256='a'*64,queries={},runtime=runtime,
        ports={'clk','fabric_clk'},word_phy_directory=tmp_path,source_hierarchy_prefix='mapped_partition')
    manifest=tmp_path/'native-session.json'
    assert load_session(manifest)['timing_query_mode']=='clock-aware-reference'
    assert load_session(manifest)['synthesis']=={'mode':'fresh','fresh_options':{
        'flatten_hierarchy':'rebuilt','gated_clock_conversion':'auto'}}
    assert 'set emuflow_timing_query_mode "clock-aware-reference"'in config.read_text()
    broken=read_json(manifest);broken['timing_query_mode']='datapath-only';write_json(manifest,broken)
    with pytest.raises(ValidationError,match='invalid Vivado native session'):
        load_session(manifest)


@pytest.mark.skipif(TCL86 is None, reason="native timing bridge requires Tcl 8.6")
@pytest.mark.parametrize("mode", ["datapath-only", "clock-aware-reference"])
@pytest.mark.parametrize("fixed_map", ["", "fixed map.tsv"])
@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize("prepared", [False, True])
def test_native_timing_bridge_forwards_measurement_policy_for_all_query_classes(tmp_path, mode, fixed_map, diagnostic, prepared):
    kinds = ("boundary_timing", "logic_segment_timing", "local_path_timing")
    output = tmp_path/"timing-diagnostic" if diagnostic else tmp_path
    output.mkdir(exist_ok=True)
    plan = ["kind\tquery_hex\toutput_hex"]
    for kind in kinds:
        query, result = output/f"{kind}.query.tsv", output/f"{kind}.result.tsv"
        query.write_text("bounded resolved query")
        plan.append("\t".join((kind, str(query).encode().hex(), str(result).encode().hex())))
    (output/"timing-exports.tsv").write_text("\n".join(plan)+"\n")
    script = f"source {_tcl_word(ROOT/'scripts/vivado/native_session.tcl')}\n"
    script += f"set emuflow_session_out {_tcl_word(tmp_path)}\nset emuflow_timing_query_mode {_tcl_word(mode)}\n"
    script += "set expected_stage "+("prepare-timing-diagnostic" if diagnostic else "prepare-timing")+"\n"
    script += f"set already_prepared {str(prepared).lower()}\n"
    script += r'''
proc emuflow_session_child {stage} {
 if {$::already_prepared} {error "repeated query preparation"}
 if {$stage ne $::expected_stage} {error "unexpected stage"}
}
proc emuflow_session_source {script arguments} {
 if {$::emuflow_native_export_stage ne "routed"} {error "wrong native snapshot"}
 puts [join [linsert $arguments 0 [file tail $script]] "\t"]
}
'''
    script += f"emuflow_session_timings scripts partition {_tcl_word(fixed_map)} {str(diagnostic).lower()} {str(prepared).lower()}\n"
    result = subprocess.run([TCL86], input=script, text=True, capture_output=True, check=True)
    assert not result.stderr
    observed = [row.split("\t") for row in result.stdout.splitlines()]
    suffix = [fixed_map, mode] if mode == "clock-aware-reference" else ([fixed_map] if fixed_map else [])
    for kind, row in zip(kinds, observed):
        exporter = "export_boundary_timing.tcl" if kind == "boundary_timing" else "export_logic_segment_timing.tcl"
        assert row == [exporter, "@current-design", str(output/f"{kind}.query.tsv"),
                       str(output/f"{kind}.result.tsv"), "partition", *suffix]
        assert (output/f"{kind}.query.tsv").exists() is diagnostic
    assert len(observed) == len(kinds)


@pytest.mark.skipif(TCL86 is None, reason="native stage bridge requires Tcl 8.6")
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("proof_mode", ["pass", "fail", "pass-diagnostic", "fail-diagnostic", "empty-diagnostic", "inventory-fail-diagnostic", "native-word", "native-word-diagnostic", "native-word-placement"])
def test_native_stage_bridge_does_not_run_source_shape_checks(tmp_path, resume, proof_mode):
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    out = Path(session["output"])
    template = tmp_path/"observed.tsv";shutil.copyfile(out/"mapped_cells.tsv", template)
    for file in (out/"mapped_cells.tsv", out/"routed_mapped_cells.tsv"):
        file.unlink()
    prior = tmp_path/"prior.dcp"
    prior.write_bytes((out/"synthesized.dcp").read_bytes())
    (out/"synthesized.dcp").unlink()
    (out/".tmp").mkdir()
    runtime = {"fabric_clock": {"period_ns": 4}, "virtual_dut_clocks": {"clk": {"target_period_ns": 20, "nominal_period_ns": 200}},
               "timing_model": {"fabric_clock_port": "fabric_clk", "dut_clock_ports": ["clk"]}}
    diagnostic = proof_mode.endswith("-diagnostic")
    native_word=proof_mode.startswith('native-word')
    roles=None
    placement=None
    if native_word:
        roles=tmp_path/'native-roles';roles.mkdir();(roles/'rx-roles.tsv').write_text('source_origin\ns0\n')
        (out/'native-word-clocks.tsv').write_text('clock_hex\tport_hex\tperiod_ns\n')
    if proof_mode=='native-word-placement':
        from emuflow.native_bank_placement import POLICY
        placement=out/'native-bank-placement';placement.mkdir()
        for name,header in [('banks.tsv','bank\tdirection\tinstance\n'),('rx-bank-roles.tsv','source_origin\tbank\n')]:
            (placement/name).write_text(header)
        write_json(placement/'request.json',dict(schema='emuflow.native-bank-placement-request/v1',status='complete',
            fpga='fpga0',policy=POLICY,directory=str(placement),input_sha256={},files={
                name:dict(path=str(placement/name),sha256=file_sha256(placement/name))for name in('banks.tsv','rx-bank-roles.tsv')}))
    config = prepare_manifest(out, fpga="fpga0", part="xcpart", mapped_index=Path(session["mapped_index"]),
        functions=Path(session["functions"]), mapped_sha256=session["mapped_index_sha256"], queries={}, runtime=runtime,
        ports={"clk", "fabric_clk"}, synthesized_checkpoint=prior if resume else None,
        synthesized_checkpoint_sha256=file_sha256(prior) if resume else None,
        retain_routed_checkpoint=diagnostic or native_word,route_after_proof_failure=diagnostic,
        word_phy_directory=out if native_word else None,word_source_directory=roles,
        source_hierarchy_prefix='mapped_partition'if native_word else'',
        bank_placement_request=placement/'request.json'if placement is not None else None)
    native = ROOT/"scripts/vivado/implement_indexed_partition.tcl"
    arguments = ["xcpart", "netlist.v", "dut", "runtime.xdc", str(out), "3", "", "", "", "", "", "Default", "Default", "1", "2", str(config)]
    script = "set argv [list "+" ".join(_tcl_word(v) for v in arguments)+"]\nset argc [llength $argv]\n"
    script += f"set template {_tcl_word(template)}\nset trace_path {_tcl_word(out/'trace.txt')}\n"
    script += f"set proof_mode {_tcl_word(proof_mode)}\n"
    script += r'''
set trace {}
rename source original_source
proc source {path} {
  if {[file tail $path] eq "source_inventory.tcl"} {
    proc emuflow_inventory {path prefix {placed 0}} {
      if {!$placed} {file copy -force $::template $path;return 2}
      if {$::proof_mode eq "inventory-fail-diagnostic"} {
        lappend ::trace inventory_failure
        error "routed source inventory unavailable"
      }
      set input [open $::template r];set output [open $path w]
      gets $input header;puts $output $header;set index 0
      while {[gets $input line]>=0} {
        puts $output [join [concat [lrange [split $line "\t"] 0 3] [list SLICE_X${index}Y0 AFF $index 0 LEAF]] "\t"]
        incr index
      }
      close $input;close $output;return $index
    }
  } elseif {[file tail $path] eq "resource_inventory.tcl"} {
    proc emuflow_resource_inventory {} {return {lut 1 ff 1 carry8 0 dsp48 0 uram288 0 ramb18_cells 0 ramb36_cells 0 routed_cells 2 physical_cells 2 infrastructure_cells 0 optimization_cells 0}}
  } elseif {[file tail $path] eq "native_session.tcl" && $::proof_mode ni {pass pass-diagnostic}} {
    uplevel 1 [list original_source $path]
    if {[string match native-word* $::proof_mode]} {
      proc emuflow_session_child {args} {error "legacy timing preparation invoked"}
      proc emuflow_session_timings {args} {error "old source endpoint export invoked"}
    }
    proc emuflow_session_frontiers {directory} {
      set ::emuflow_native_export_stage optimized
      if {$::proof_mode eq "empty-diagnostic"} {error ""}
      error "unresolved selected-state frontier"
    }
  } elseif {[string match native-word* $::proof_mode] && [file tail $path] eq "word_native_physical.tcl"} {
    proc emuflow_word_prepare {args} {}
    proc emuflow_word_routed_checks {args} {error "legacy RX hierarchy scope invoked"}
    proc emuflow_word_write_control_metrics {args} {error "legacy control scope invoked"}
  } elseif {[string match native-word* $::proof_mode] && [file tail $path] eq "word_data_budgets.tcl"} {
    proc emuflow_word_data_budgets {args} {return {100.0 1}}
  } elseif {$::proof_mode eq "native-word-placement" && [file tail $path] eq "native_word_timing.tcl"} {
    proc emuflow_native_word_source_roles {path} {
      if {[info exists ::emuflow_native_export_stage]} {error "placement masqueraded as a routed measurement"}
      return structural_roles
    }
  } elseif {$::proof_mode eq "native-word-placement" && [file tail $path] eq "native_bank_placement.tcl"} {
    proc emuflow_native_bank_placement {directory sources output prefix} {
      if {$sources ne "structural_roles" || [lindex $::trace end] ne "opt"} {error "placement policy ran before optimization"}
      lappend ::trace bank_placement
    }
    proc emuflow_native_bank_replicate {directory sources output prefix} {
      if {$sources ne "structural_roles" || [lindex $::trace end] ne "place"} {error "control replication did not follow placement"}
      lappend ::trace bank_replication
    }
  } else {uplevel 1 [list original_source $path]}
}
rename exit process_exit
proc exit {status} {set f [open $::trace_path w];puts $f $::trace;close $f;process_exit $status}
proc set_param {args} {}
proc get_parts {args} {return xcpart}
proc current_design {args} {return dut}
proc create_project {args} {}
proc read_verilog {args} {lappend ::trace read_verilog}
proc read_xdc {args} {}
proc synth_design {args} {lappend ::trace synth}
proc open_checkpoint {path} {lappend ::trace open}
proc get_pblocks {args} {return {}}
proc get_cells {args} {return {}}
proc get_ports {args} {return [lindex $args end]}
proc get_clocks {args} {
 if {[lsearch -exact $args -of_objects]>=0} {
  return [expr {[lindex $args end] eq "fabric_clk" ? "emuflow_tdm_clk" : "emuflow_dut_clk_0"}]
 }
 if {[llength $args]==1} {return {emuflow_tdm_clk emuflow_dut_clk_0}}
 set pattern [lindex $args end]
 return [lsearch -all -inline -glob {emuflow_tdm_clk emuflow_dut_clk_0} $pattern]
}
proc get_property {property object} {
 switch $property {
  PART {return xcpart}
  NAME {return [expr {$object eq "dut" ? "checkpoint_synthesized" : $object}]}
  TOP {return dut}
  PERIOD {return [expr {$object eq "emuflow_tdm_clk" ? 4.0 : 20.0}]}
  SLACK {return 0.5}
  DATAPATH_DELAY {return 2.5}
 }
 error "unexpected mock property $property"
}
proc write_checkpoint {args} {set p [lindex $args end];lappend ::trace [file tail $p];set f [open $p w];puts $f snapshot;close $f}
proc opt_design {} {lappend ::trace opt}
proc place_design {args} {
  if {[info exists ::emuflow_native_export_stage]} {error "stale optimized export stage leaked into placement"}
  lappend ::trace place
}
proc phys_opt_design {} {lappend ::trace phys_opt}
proc route_design {args} {lappend ::trace route}
foreach cmd {report_route_status report_drc report_timing_summary report_utilization report_design_analysis} {
 proc $cmd {args} {set i [lsearch -exact $args -file];set f [open [lindex $args [expr {$i+1}]] w];puts $f report;close $f}
}
proc get_drc_violations {args} {return {}}
proc get_nets {args} {return {}}
proc get_timing_paths {args} {return path}
proc version {args} {return 2024.2}
# The child must use its original Python runtime rather than these vendor vars.
set env(PYTHONHOME) /invalid-vendor-python
set env(PYTHONPATH) /invalid-vendor-package
'''
    script += f"source {_tcl_word(native)}\n"
    completed = subprocess.run([TCL86], input=script, text=True, capture_output=True)
    trace = (out/"trace.txt").read_text().split()
    assert ("synth" in trace) is (not resume)
    assert ("open" in trace) is resume
    assert "placed.dcp" not in trace
    if native_word:
        assert completed.returncode==0,completed.stdout+completed.stderr
        assert (out/'routed.dcp').is_file()and(out/'implementation_metrics.tsv').is_file()
        assert not(out/'routed-query-preparation.json').exists()and not(out/'timing-exports.tsv').exists()
        assert read_json(out/'native-session.json')['queries']=={}
        assert read_json(out/'native-session.json')['word_data_budget']['source_origins']==1
        assert 'EMUFLOW_INDEXED_VIVADO status=pass'in completed.stdout
        if placement is not None:
            assert trace[-5:]==['place','bank_replication','phys_opt','route','routed.dcp']
            assert trace.index('opt')<trace.index('bank_placement')<trace.index('place')
        else:
            assert trace[-4:]==['place','phys_opt','route','routed.dcp']
        return
    if proof_mode == "inventory-fail-diagnostic":
        assert completed.returncode != 0
        assert trace[-2:] == ["routed.dcp", "inventory_failure"]
        assert (out/"routed.dcp").is_file()
        assert (out/"route_status.rpt").is_file() and (out/"timing_summary.rpt").is_file()
        assert not (out/"routed-query-preparation.json").exists()
        return
    assert completed.returncode == 0, completed.stdout+completed.stderr
    assert read_json(out/"routed-query-preparation.json")["status"] == "pass"
    assert not (out/"optimization-proofs.sqlite").exists()
    assert not (out/"source-proof-failure.txt").exists()
    assert "EMUFLOW_SOURCE_PROOF" not in completed.stdout
    assert "EMUFLOW_INDEXED_VIVADO status=pass" in completed.stdout
    if diagnostic:
        assert trace[trace.index("opt"):] == ["opt", "optimized.dcp", "place", "phys_opt", "route", "routed.dcp"]
        assert (out/"optimized.dcp").is_file() and (out/"routed.dcp").is_file()
    else:
        assert trace[-4:] == ["opt", "place", "phys_opt", "route"]


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_synthesized_module_identity_is_independent_of_checkpoint_container_name():
    helper = ROOT/"scripts/vivado/native_session.tcl"
    script = f"source {_tcl_word(helper)}\n"+r'''
set part xcpart
set top actual_module
proc current_design {args} {return checkpoint_renamed_snapshot}
proc get_property {property object} {
 if {$property eq "PART"} {return $::part}
 if {$property eq "TOP"} {return $::top}
 if {$property eq "NAME"} {return checkpoint_renamed_snapshot}
 error "unexpected property $property"
}
emuflow_validate_synthesized_identity xcpart actual_module
set top wrong_module
if {![catch {emuflow_validate_synthesized_identity xcpart actual_module}]} {error "wrong module accepted"}
set top actual_module
set part wrong_part
if {![catch {emuflow_validate_synthesized_identity xcpart actual_module}]} {error "wrong part accepted"}
puts pass
'''
    result = subprocess.run([shutil.which("tclsh")],input=script,text=True,capture_output=True,check=True)
    assert not result.stderr
    assert result.stdout.strip() == "pass"


@pytest.mark.parametrize("dependent", [False, True])
def test_postopt_preflight_confirms_states_removed_only_after_synthesis(tmp_path, dependent):
    session, facts, origins = session_fixture(tmp_path, dependent=dependent)
    session["source_preflight_stage"] = "optimized"
    out = Path(session["output"])
    shutil.copyfile(out/"mapped_cells.tsv", out/"optimized_mapped_cells.tsv")
    # These endpoints existed in the synthesis snapshot and disappeared in opt_design.
    with (out/"mapped_cells.tsv").open("a") as stream:
        stream.write(f"p/conditional\tFDRE\ts{origins['conditional']}\t\t\t\t\t\t\n")
    result = prepare_frontiers(session)
    assert result["constant_sources"] == (2 if dependent else 1)
    assert read_json(out/"optimized-source-preflight.json")["source_stage"] == "optimized"
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    confirm_frontiers(session)
    assert read_json(out/"checked-constant-frontiers.json")["source_stage"] == "optimized"
    prepare_checked_timing(session)
    with OptimizationProofView(out/"optimization-proofs.sqlite", expected_fpga="fpga0") as proof:
        assert proof.require_eliminated_source(f"s{origins['conditional']}")["initialization"] == "vendor-confirmed"
    assert not (out/"optimized_mapped_cells.tsv").exists()


def test_postopt_preflight_rejects_a_facts_stage_mismatch(tmp_path):
    session, facts, _ = session_fixture(tmp_path)
    out = Path(session["output"])
    prepare_frontiers(session)
    shutil.copyfile(facts, out/"constant-frontiers.tsv")
    session["source_preflight_stage"] = "optimized"
    with pytest.raises(ValidationError, match="different preplacement stage"):
        confirm_frontiers(session)


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="Tcl interpreter required")
def test_native_child_preserves_explicit_runtime_installation(tmp_path, monkeypatch):
    selected = tmp_path/'selected runtime [1]'
    selected.mkdir()
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(selected))
    out = tmp_path/'out';out.mkdir()
    runtime = {"fabric_clock":{"period_ns":5},"virtual_dut_clocks":{"clk":{"target_period_ns":100}},
               "timing_model":{"fabric_clock_port":"fabric_clk","dut_clock_ports":["clk"]}}
    config = prepare_manifest(out, fpga="fpga0", part="part", mapped_index=tmp_path/'mapped',
        functions=tmp_path/'functions', mapped_sha256='a'*64, queries={}, runtime=runtime, ports={"clk","fabric_clk"})
    code = f"source {_tcl_word(config)}\nputs [lsearch -inline $emuflow_child_command {{EMUFLOW_NATIVE_ROOT=*}}]\n"
    result = subprocess.run([shutil.which("tclsh")], input=code, text=True, capture_output=True, check=True)
    assert not result.stderr
    assert result.stdout.strip() == 'EMUFLOW_NATIVE_ROOT='+str(selected)


@pytest.mark.skipif(TCL86 is None, reason="Tcl 8.6 required")
@pytest.mark.parametrize("condition", ["routed", "wrong-part", "incomplete-route"])
@pytest.mark.parametrize("word_mode", [False, True])
def test_retained_checkpoint_exports_without_reimplementation(tmp_path, condition, word_mode):
    from emuflow.vivado_session import prepare_routed_timing_export
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    prior = Path(session["output"])
    session.update(source_hierarchy_prefix="", queries={}, implementation_timing={"dut_clock_periods_ns":{"clk":100}})
    if word_mode:
        session.update(source_hierarchy_prefix="partition", word_phy_directory=str(prior), timing_query_mode="clock-aware-reference")
        boundary = prior/"boundary-query.tsv"
        boundary.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n")
        session["queries"]["boundary_timing"] = str(boundary)
    write_json(prior/"native-session.json", session)
    (prior/"routed.dcp").write_bytes(b"retained routed physical design")
    (prior/"clock-contract.tsv").write_text("clock_hex\tport_hex\tperiod_ns\n"+b"core_clk".hex()+"\t"+b"clk".hex()+"\t100\n")
    (prior/"fixed-io-timing-map.tsv").write_text("logical_hex\tkind\tphysical_hex\tanchor_hex\n")
    parents = {path:file_sha256(path) for path in prior.iterdir() if path.is_file()}
    out = tmp_path/"export"
    config = prepare_routed_timing_export(prior/"native-session.json", out)
    recovered = read_json(out/"native-session.json")
    assert recovered["routed_export"]["checkpoint_sha256"] == file_sha256(prior/"routed.dcp")
    assert recovered["implementation_timing"] == session["implementation_timing"]
    assert (out/"clock-contract.tsv").read_bytes() == (prior/"clock-contract.tsv").read_bytes()
    program = "set argv [list "+_tcl_word(config)+"]\nset argc 1\n"
    program += "set template "+_tcl_word(prior/"routed_mapped_cells.tsv")+"\n"
    program += "set condition "+_tcl_word(condition)+"\n"
    program += "set expected_part "+_tcl_word(session["part"])+"\n"
    program += r'''
rename source original_source
proc source {path} {
  if {[file tail $path] eq "source_inventory.tcl"} {
    proc emuflow_inventory {path prefix placed} {file copy $::template $path}
  } elseif {[file tail $path] eq "word_native_physical.tcl"} {
    uplevel 1 [list original_source $path]
    proc emuflow_word_routed_checks {directory scope} {
      if {![file exists "$directory/routed-query-preparation.json"]} {error "RX scope queried before preparation"}
      return {2 -0.2 2 3 0 -0.3}
    }
  } else {uplevel 1 [list original_source $path]}
}
proc set_param {args} {}
proc set_max_delay {args} {}
proc open_checkpoint {path} {puts "opened [file tail $path]"}
proc current_design {args} {return dut}
proc get_property {property object} {
  switch $property {
    PART {return [expr {$::condition eq "wrong-part" ? "another-part" : $::expected_part}]}
    NAME {return core_clk}
    PERIOD {return 100}
  }
  error "unexpected property $property"
}
proc get_ports {args} {return clk}
proc get_clocks {args} {return core_clk}
proc get_nets {args} {return [expr {$::condition eq "incomplete-route" ? "unrouted" : ""}]}
foreach command {report_route_status report_drc report_timing_summary report_clocks} {
  proc $command {args} {set f [open [lindex $args end] w];puts $f "retained report";close $f}
}
'''
    program += "source "+_tcl_word(ROOT/"scripts/vivado/export_routed_session.tcl")+"\n"
    completed = subprocess.run([TCL86], input=program, text=True, capture_output=True)
    assert (completed.returncode == 0) is (condition == "routed"), completed.stdout+completed.stderr
    assert "opened routed.dcp" in completed.stdout
    assert parents == {path:file_sha256(path) for path in parents}
    if condition == "routed":
        assert "EMUFLOW_ROUTED_TIMING status=exported" in completed.stdout
        assert read_json(out/"routed-query-preparation.json")["timing_coverage"]["status"] == "complete"
        assert not (out/"constant-frontier-plan.json").exists()
        assert not (out/"optimization-proofs.sqlite").exists()
        if word_mode:
            with (out/"word-control-metrics.tsv").open() as stream:
                metrics = dict(csv.reader(stream, delimiter="\t"))
            assert metrics["native_word_infrastructure_wns_ns"] == "-0.2"
            assert metrics["native_word_infrastructure_whs_ns"] == "-0.3"
            assert metrics["native_word_rx_scope_complete"] == "0"
        else:
            assert not (out/"word-control-metrics.tsv").exists()
        with pytest.raises(FileExistsError):
            prepare_routed_timing_export(prior/"native-session.json", out)
    else:
        assert not (out/"routed-query-preparation.json").exists()


def test_routed_export_rejects_a_changed_retained_checkpoint(tmp_path):
    from emuflow.vivado_session import prepare_routed_timing_export, load_session
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    prior = Path(session["output"])
    session["source_hierarchy_prefix"] = ""
    write_json(prior/"native-session.json", session)
    (prior/"clock-contract.tsv").write_text("clock_hex\tport_hex\tperiod_ns\n")
    with pytest.raises(ValidationError, match="retained routed.dcp"):
        prepare_routed_timing_export(prior/"native-session.json", tmp_path/"absent")
    (prior/"routed.dcp").write_bytes(b"retained")
    config = prepare_routed_timing_export(prior/"native-session.json", tmp_path/"export")
    (prior/"routed.dcp").write_bytes(b"changed")
    with pytest.raises(ValidationError, match="checkpoint changed"):
        prepare_timing(load_session(config.with_suffix(".json")))


def test_retained_checkpoint_can_measure_a_new_complete_query_library(tmp_path):
    from emuflow.vivado_session import prepare_routed_timing_export, load_session
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    prior = Path(session["output"])
    session["source_hierarchy_prefix"] = ""
    write_json(prior/"native-session.json", session)
    (prior/"routed.dcp").write_bytes(b"retained physical design")
    (prior/"clock-contract.tsv").write_text("clock_hex\tport_hex\tperiod_ns\n")
    original_query = Path(session["queries"]["local_path_timing"])
    complete = tmp_path/"complete-query.tsv"
    complete.write_text(original_query.read_text().replace(b"path0".hex(), b"new-source-member".hex()))
    parents = {p: file_sha256(p) for p in [prior/"native-session.json", prior/"routed.dcp", original_query, complete]}
    out = tmp_path/"complete-export"
    config = prepare_routed_timing_export(prior/"native-session.json", out,
        query_overrides={"local_path_timing": complete})
    recovered = load_session(config.with_suffix(".json"))
    shutil.copyfile(prior/"routed_mapped_cells.tsv", out/"routed_mapped_cells.tsv")
    assert prepare_timing(recovered)["timing_coverage"] == {
        "status":"complete", "requested_queries":1, "measurable_queries":1, "unresolved_queries":0}
    rows = list(csv.DictReader((out/"local_path_timing-resolved.tsv").open(), delimiter="\t"))
    assert len(rows) == 1 and bytes.fromhex(rows[0]["endpoint_hex"]).decode() == "new-source-member"
    assert recovered["routed_export"]["query_overrides"] == {
        "local_path_timing":{"path":str(complete), "sha256":file_sha256(complete)}}
    assert parents == {p: file_sha256(p) for p in parents}


def test_complete_local_queries_can_be_extracted_independently_of_word_boundaries(tmp_path):
    from emuflow.vivado_session import prepare_routed_timing_export, load_session
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    prior = Path(session["output"])
    session.update(source_hierarchy_prefix="partition", word_phy_directory=str(prior), timing_query_mode="clock-aware-reference")
    boundary = tmp_path/"boundary-query.tsv"; boundary.write_text("separate canonical boundary query")
    session["queries"]["boundary_timing"] = str(boundary)
    write_json(prior/"native-session.json", session)
    (prior/"routed.dcp").write_bytes(b"completed physical implementation")
    (prior/"clock-contract.tsv").write_text("clock_hex\tport_hex\tperiod_ns\n")
    original = Path(session["queries"]["local_path_timing"])
    complete = tmp_path/"complete-local-query.tsv"
    complete.write_text(original.read_text().replace(b"path0".hex(), b"complete-local-member".hex()))
    parents = {p:file_sha256(p) for p in [prior/"native-session.json", prior/"routed.dcp", boundary, original, complete]}
    out = tmp_path/"local-export"
    config = prepare_routed_timing_export(prior/"native-session.json", out,
        query_overrides={"local_path_timing":complete}, query_kinds=["local_path_timing"])
    recovered = load_session(config.with_suffix(".json"))
    assert recovered["queries"] == {"local_path_timing":str(complete)}
    assert recovered["routed_export"]["query_kinds"] == ["local_path_timing"]
    assert 'set emuflow_allow_generated_clocks "true"' in config.read_text()
    assert 'set emuflow_export_word_control_timing "false"' in config.read_text()
    shutil.copyfile(prior/"routed_mapped_cells.tsv", out/"routed_mapped_cells.tsv")
    assert prepare_timing(recovered)["timing_coverage"] == {"status":"complete",
        "requested_queries":1, "measurable_queries":1, "unresolved_queries":0}
    assert parents == {p:file_sha256(p) for p in parents}


@pytest.mark.parametrize("selection", [[], ["missing"], ["local_path_timing", "local_path_timing"]])
def test_invalid_routed_query_selection_does_not_create_an_export(tmp_path, selection):
    from emuflow.vivado_session import prepare_routed_timing_export
    session, _, _ = session_fixture(tmp_path)
    prior = Path(session["output"]); session["source_hierarchy_prefix"] = ""
    write_json(prior/"native-session.json", session)
    (prior/"routed.dcp").write_bytes(b"retained routed design")
    out = tmp_path/"invalid-export"
    with pytest.raises(ValidationError, match="query selection"):
        prepare_routed_timing_export(prior/"native-session.json", out, query_kinds=selection)
    assert not out.exists()
