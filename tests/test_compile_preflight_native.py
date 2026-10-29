from tests.preflight_reference import _initial_context
"""Whole native admission differential; controlled executables are not design qualification."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from emuflow.flow.native_compile import _request
from emuflow.flow.native_compile_adapter import _control_snapshot, _unpack
from emuflow.common.native_tools import native_file_generation
from tests.native_build import emuflow_native
from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract


def request_options(root,backend=None):
    rtl=root/'top.v';rtl.write_text('module top(input clk); endmodule\n')
    sta=root/'sta';sta.write_text('#!/bin/sh\nif [ "$1" = -version ]; then echo 3.1.0; else echo "EMUFLOW_OPENSTA_RUNTIME status=pass tcl=8.6.16"; fi\n');sta.chmod(0o700)
    options=dict(output_dir=root/'native',sources=[rtl],top='top',clocks=['clk'],clock_periods={'clk':10.0},
        opensta=str(sta),openroad=sys.executable,yosys=sys.executable,
        architecture_timing_db=write_mock_xilinx_timing_contract(root/'timing.json'))
    if backend is None:return options
    options.update(physical=True,physical_backend=backend,global_sta_executable=str(sta))
    if backend=='vivado':options['physical_vivado']=sys.executable;return options
    from tests.test_openparf_runtime_native import executable
    tool=root/'solver';tool.mkdir();runtime,_,_=executable(tool)
    device=root/'device';timing=root/'delays'
    for path in (device/'data/parts.db',device/'data/devices/virtexuplus/xcvu19p_db.dat',
            timing/'intersite_delay_terms.txt',timing/'intrasite_delay_terms.txt',
            root/'architecture.json',root/'native.json',root/'provider.json',root/'package.jar'):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}')
    java=root/'bin/java';java.parent.mkdir();java.write_text('#!/bin/sh\nexit 0\n');java.chmod(0o700)
    javac=java.with_name('javac');javac.write_bytes(java.read_bytes());javac.chmod(0o700)
    options.update(physical_architecture=root/'architecture.json',physical_openparf_install=runtime['install_root'],
        physical_openparf_python=runtime['python_executable'],physical_rapidwright_jar=root/'package.jar',
        physical_rapidwright_java=java,physical_rapidwright_device_data=device,physical_rapidwright_timing_data=timing,
        physical_rapidwright_opensta=str(sta),physical_rapidwright_native_constraints=root/'native.json',
        physical_rapidwright_provider_manifest=root/'provider.json')
    return options


def run(root,value,state=None):
    source=root/'request.json';source.write_text(json.dumps(value));state=root/'state.json' if state is None else state;proof=root/'operation.json'
    result=subprocess.run([str(emuflow_native()),'--report',str(proof),'compile','prepare',str(source),str(state),'test'],capture_output=True,text=True,timeout=60)
    operation=json.loads(proof.read_text()) if proof.exists() else json.loads(result.stderr.split('emuflow-native: ',1)[1]);return result,state,operation


def normalized(value,output):
    if isinstance(value,Path):return normalized(str(value),output)
    if isinstance(value,(list,tuple)):return [normalized(v,output) for v in value]
    if isinstance(value,dict):return {k:('RECEIPT' if k.endswith('_generation') else normalized(v,output)) for k,v in value.items()}
    if isinstance(value,str) and (value==str(output) or value.startswith(str(output)+'/')):return '$OUTPUT'+value[len(str(output)):]
    return value


@pytest.mark.parametrize('backend',[None,'vivado','rapidwright'])
def test_all_static_configurations_and_effective_state_match_independent_preflight(tmp_path,backend):
    from tests.preflight_reference import _compile_preflight
    from emuflow.flow import multi_fpga_flow as flow
    options=request_options(tmp_path,backend);value,_=_request((),options)
    reference_value=copy.deepcopy(value);reference_value['options']['output_dir']=str(tmp_path/'reference')
    ctx=_initial_context(reference_value);_compile_preflight(ctx)
    expected=_control_snapshot(ctx,'preflight',flow)
    result,state,operation=run(tmp_path,value)
    assert result.returncode==0,operation['error']
    actual=_unpack(json.loads(state.read_text())['state'])
    assert normalized(actual,tmp_path/'native')==normalized(expected,tmp_path/'reference')
    for name in ('native_phase2_configuration','native_phase3_configuration','native_phase4_configuration','native_phase6_configuration','native_physical_policy'):
        if name in expected:
            assert normalized(json.loads(actual[name].read_text()),tmp_path/'native')==normalized(json.loads(expected[name].read_text()),tmp_path/'reference')
    assert operation['stage']=='preflight' and operation['status']=='pass'
    assert not list((tmp_path/'native').glob('.compile-admission-*'))
    assert json.loads(state.read_text())['request_generation']==list(map(str,native_file_generation(tmp_path/'request.json')))


@pytest.mark.parametrize('name,bad',[
    ('patron_algorithm_version',True),('patron_algorithm_version',13),('partition_provider','retired'),
    ('optimize_frame_slots',True),('global_timing_engine','python'),('global_sta_executable','/missing'),
    ('mapping_profile','retired'),('xilinx_mapping_strategy','retired'),('tdm_provider','retired'),
    ('cut_mode','sequential-only'),('route_provider','retired'),('phase6_provider','retired'),
    ('phase6_chimew_region_count',1),('phase6_chimew_region_count',32),('phase6_chimew_region_count',True),
    ('architecture_timing_db',None),('clock_periods',None),('clock_periods',{}),('sources',['relative']),
    ('route_candidate_workers',True),('physical',1),('output_dir','relative'),('unknown_option',1)])
def test_native_admission_rejects_invalid_configuration_without_output_publication(tmp_path,name,bad):
    value,_=_request((),request_options(tmp_path));value['options'][name]=bad
    result,state,operation=run(tmp_path,value)
    assert result.returncode!=0 and operation['status']=='fail'
    assert not state.exists() and not (tmp_path/'native').exists()
    assert not list(tmp_path.glob('.compile-admission-*'))


def test_default_option_contract_and_required_tools_are_native_inputs(tmp_path):
    options=request_options(tmp_path);value,_=_request((),options)
    value['options']={k:v for k,v in value['options'].items() if k in options}
    result,state,operation=run(tmp_path,value)
    assert result.returncode==0,operation['error']
    effective=json.loads(state.read_text())['effective_options']
    assert effective['partition_provider']=='patron' and effective['physical_seed']==1
    assert any(row['schema']=='compile-options-v1' for row in operation['inputs'])
    assert any(row['schema']=='frontend-source-v1' for row in operation['inputs'])


def test_control_state_cannot_overwrite_a_published_configuration(tmp_path):
    value,_=_request((),request_options(tmp_path))
    result,state,operation=run(tmp_path,value,tmp_path/'native/.native-phase3-configuration/request.json')
    assert result.returncode!=0 and operation['status']=='fail'
    assert not (tmp_path/'native').exists()


def test_request_alias_rolls_back_all_stage_directories(tmp_path):
    value,_=_request((),request_options(tmp_path))
    result,source,operation=run(tmp_path,value,tmp_path/'request.json')
    assert result.returncode!=0 and operation['status']=='fail'
    assert json.loads(source.read_text())==value
    assert not list((tmp_path/'native').iterdir())


def test_tool_probe_cannot_change_an_already_bound_board(tmp_path):
    options=request_options(tmp_path)
    from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH
    board=tmp_path/'board.json';board.write_bytes(DEFAULT_PLATFORM_PATH.read_bytes());options['platform_path']=board
    engine=Path(options['opensta'])
    engine.write_text('#!'+sys.executable+'\n'+f"""
import sys
from pathlib import Path
if '-version' in sys.argv:
 p=Path({str(board)!r});p.write_text(p.read_text()+'\\n');print('3.1.0')
else:print('EMUFLOW_OPENSTA_RUNTIME status=pass tcl=8.6.16')
""")
    value,_=_request((),options);result,state,operation=run(tmp_path,value)
    assert result.returncode!=0 and operation['status']=='fail'
    assert not state.exists() and not (tmp_path/'native').exists()


@pytest.mark.parametrize('schema',['opensta-input-export-request-v1','routed-endpoint-request-v1',
    'routed-opensta-request-v1','vivado-lineage-store-v2','routed-segment-request-v1','vivado-export-request-v1'])
def test_same_native_runtime_owns_required_contracts_and_rejects_unknown_versions(schema):
    native=str(emuflow_native())
    valid=subprocess.run([native,'check-runtime',schema],capture_output=True,text=True)
    assert valid.returncode==0,valid.stderr
    invalid=subprocess.run([native,'check-runtime',schema+'-unknown'],capture_output=True,text=True)
    assert invalid.returncode!=0
    assert schema+'-unknown' in invalid.stderr
