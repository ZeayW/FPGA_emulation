"""Maintained native frontend APIs and retirement of the Python stage chain.

Small component fixtures here do not replace NVDLA qualification.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import native_file_generation
from emuflow_tools.frontend.inputs import run_supplied_frontend
from emuflow_tools.frontend.frontend_native import run_rtl_frontend
from emuflow.ir.ir import EmuIR
from emuflow.ir.emuir_storage import read_indexed_emuir_identity
from emuflow.flow.cli import _build_parser

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT/'platforms/virtual/xcvu3p_2fpga_p2p.json'
SOURCE = ROOT/'examples/yosys/counter.json'


def test_replaced_python_stage_and_its_cli_are_retired():
    assert importlib.util.find_spec('emuflow.frontend.phase1') is None
    with pytest.raises(SystemExit) as error:
        _build_parser().parse_args(['phase1', '--help'])
    assert error.value.code == 2


def test_supplied_frontend_uses_native_owner_and_canonical_artifact_layout(tmp_path):
    generation = native_file_generation(SOURCE)
    with patch('emuflow.ir.ir.EmuIR.load', side_effect=AssertionError('host graph')):
        summary = run_supplied_frontend(SOURCE, BOARD, tmp_path/'out', top='counter', clocks=['clk'])
    assert native_file_generation(SOURCE) == generation
    assert summary['design'] == 'counter' and summary['status'] == 'pass'
    assert set(summary) == {'schema','phase','status','design','platform','synthesis'}
    stage = tmp_path/'out/frontend/phase1'
    assert read_indexed_emuir_identity(stage/'design.emuir.sqlite') == ('counter', 8, 10)
    report = json.loads((stage/'phase1_report.json').read_text())
    assert report['fits_on_platform'] and report['fit_scope'] == 'single_fpga'
    assert report['clock_topology']['fabric_logic_clock_nets'] == 0
    assert report['indexed_staging_transform']['input_sha256'] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert not list(tmp_path.glob('.frontend-input-host-*'))
    assert not (tmp_path/'out/.native-frontend-input-request').exists()


def test_native_input_normalizes_inverter_without_old_stream_callback(tmp_path):
    value = {'modules':{'top':{'ports':{'a':{'direction':'input','bits':[2]},'y':{'direction':'output','bits':[3]}},
        'cells':{'inv':{'type':'INV','parameters':{},'port_directions':{'I':'input','O':'output'},'connections':{'I':[2],'O':[3]}}},
        'netnames':{'a':{'bits':[2]},'y':{'bits':[3]}}}}}
    source=tmp_path/'inverter.json';source.write_text(json.dumps(value))
    result=run_supplied_frontend(source,BOARD,tmp_path/'out',top='top')
    assert result['status']=='pass'
    ir=EmuIR.load(tmp_path/'out/frontend/phase1/design.emuir.sqlite').value
    assert [(cell['id'],cell['type']) for cell in ir['instances']]==[('inv','LUT1')]


def test_native_input_preserves_aggregate_platform_capacity_semantics(tmp_path):
    board={'schema':'emuflow.boarddb/v1','platform':{'name':'tiny','kind':'virtual'},
        'fpgas':[{'id':name,'part':'test','capacity':{'lut':3,'ff':3},'utilization_limit':1.0} for name in ('fpga0','fpga1')],
        'links':[{'id':'l','endpoints':['fpga0','fpga1'],'direction':'full_duplex','mode':'abstract',
                  'data_lanes_per_direction':1,'fabric_clock_mhz':1,'latency_cycles':0}]}
    platform=tmp_path/'board.json';platform.write_text(json.dumps(board))
    assert run_supplied_frontend(SOURCE,platform,tmp_path/'out',clocks=['clk'])['status']=='pass'
    report=json.loads((tmp_path/'out/frontend/phase1/phase1_report.json').read_text())
    assert not any(report['fits_on_fpga'].values())
    assert report['fits_on_platform'] and report['fit_scope']=='aggregate_platform'


@pytest.mark.parametrize('damage',['capacity','unsupported-cell','existing'])
def test_native_input_failure_preserves_sources_and_previous_publication(tmp_path,damage):
    source=tmp_path/'counter.json';source.write_bytes(SOURCE.read_bytes())
    board=json.loads(BOARD.read_text());platform=tmp_path/'board.json'
    if damage=='capacity':
        for fpga in board['fpgas']:
            fpga['capacity']={name:0 for name in fpga['capacity']}
    if damage=='unsupported-cell':
        value=json.loads(source.read_text())
        next(iter(value['modules']['counter']['cells'].values()))['type']='RETIRED_BACKEND_CELL'
        source.write_text(json.dumps(value))
    platform.write_text(json.dumps(board))
    output=tmp_path/'out';stage=output/'frontend/phase1'
    if damage=='existing':
        stage.mkdir(parents=True);(stage/'previous').write_text('accepted')
    generation=native_file_generation(source)
    with pytest.raises(EmuFlowError):
        run_supplied_frontend(source,platform,output,clocks=['clk'])
    assert native_file_generation(source)==generation
    if damage=='existing':assert (stage/'previous').read_text()=='accepted'
    else:assert not stage.exists()
    assert not list(tmp_path.glob('.frontend-input-host-*'))


def rtl_engine(tmp_path):
    import sys
    source=tmp_path/'counter.v';source.write_text('module counter(input clk);endmodule\n')
    engine=tmp_path/'yosys.py'
    engine.write_text(f'#!{sys.executable}\nimport sys\nfrom pathlib import Path\n'
        'assert sys.argv[1:3]==["-q","-p"]\n'
        f'sys.stdout.write(Path({str(SOURCE)!r}).read_text())\n')
    engine.chmod(0o755)
    return source,engine


def test_optional_rtl_api_uses_whole_native_frontend(tmp_path):
    source,engine=rtl_engine(tmp_path)
    with patch('emuflow.ir.ir.EmuIR.load',side_effect=AssertionError('host graph')):
        result=run_rtl_frontend([source],'counter',BOARD,tmp_path/'out',clocks=['clk'],yosys=str(engine))
    assert result['status']=='pass'
    stage=tmp_path/'out/frontend/phase1'
    assert read_indexed_emuir_identity(stage/'design.emuir.sqlite')[0]=='counter'
    assert (stage/'yosys-execution.json').is_file()
    assert not (tmp_path/'out/frontend/native-frontend-request.bin').exists()
    assert not list(tmp_path.glob('.frontend-rtl-host-*'))


def test_standalone_benchmark_reuses_the_native_frontend_and_report_references(tmp_path):
    from emuflow_tools.benchmark import run_benchmark
    source,engine=rtl_engine(tmp_path)
    spec=tmp_path/'benchmark.json'
    spec.write_text(json.dumps({'schema':'emuflow.benchmark-run/v1','id':'fixture','design_id':'fixture',
        'top':'counter','sources':[source.name],'clocks':['clk'],'platform':str(BOARD),
        'synthesis':{'family':'xcup','policy':'native'}}))
    output=tmp_path/'out'
    report=run_benchmark(spec,tmp_path,output,yosys=str(engine))
    assert report['schema']=='emuflow.benchmark-report/v2' and report['status']=='pass'
    assert report['phase1']['design']=='counter'
    assert all((output/path).is_file() for path in report['artifacts'].values())
    assert (output/report['synthesis']['execution']).is_file()
    assert not (output/'synthesis').exists()
    assert not (output/'phase1').exists()


def test_standalone_benchmark_cannot_silently_use_a_retired_synthesis_policy(tmp_path):
    from emuflow_tools.benchmark import run_benchmark
    source,engine=rtl_engine(tmp_path)
    spec=tmp_path/'benchmark.json'
    spec.write_text(json.dumps({'schema':'emuflow.benchmark-run/v1','id':'fixture','design_id':'fixture',
        'top':'counter','sources':[source.name],'clocks':['clk'],'platform':str(BOARD),
        'synthesis':{'family':'xcup','policy':'logic-only'}}))
    with patch('emuflow_tools.benchmark.run_rtl_frontend',side_effect=AssertionError('unrequested policy substitution')):
        with pytest.raises(EmuFlowError,match='maintained xcup/native'):
            run_benchmark(spec,tmp_path,tmp_path/'out',yosys=str(engine))
    assert not (tmp_path/'out').exists()


def test_supplied_frontend_rejects_original_source_changed_after_producer_exit(tmp_path):
    import emuflow_tools.frontend.inputs as inputs
    source=tmp_path/'source.json';source.write_bytes(SOURCE.read_bytes())
    original=inputs.read_frontend_result
    def changed(*args):
        result=original(*args)
        source.write_text(source.read_text()+' ')
        return result
    with patch.object(inputs,'read_frontend_result',side_effect=changed):
        with pytest.raises(EmuFlowError,match='original source generation changed'):
            run_supplied_frontend(source,BOARD,tmp_path/'out',clocks=['clk'])
