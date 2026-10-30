"""Native fixed-I/O and clock inputs: wiring correctness and atomic rejection."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from emuflow.common.json_io import file_sha256
from emuflow.common.native_tools import native_file_generation
from tests.vivado_input_host import prepare_vivado_inputs
from tests.native_build import emuflow_native
from tests.test_vivado_indexed_physical import fixture
from tests.test_vivado_backend import RUNTIME


def fixed_contract(root):
    adapter, xdc = root/'adapter.sv', root/'pins.xdc'
    adapter.write_text('module adapter(output wire link_p,link_n,input wire [0:0] q); endmodule\n')
    xdc.write_text('# fixed wiring\n')
    return {'status':'pass', 'adapter':{'path':str(adapter), 'sha256':file_sha256(adapter), 'module':'adapter'},
            'xdc':{'path':str(xdc), 'sha256':file_sha256(xdc)},
            'metrics':{'physical_ports':2, 'differential_pairs':1},
            'entries':[{'direction':'tx', 'logical_port':'q', 'logical_bit':0, 'logical_port_width':1,
                        'p':{'port':'link_p', 'package_pin':'P1'},
                        'n':{'port':'link_n', 'package_pin':'P2'}, 'iostandard':'LVDS'}]}


def test_native_inputs_connect_exact_clock_and_fixed_port_identities(tmp_path):
    source = fixture(tmp_path)
    contract = fixed_contract(tmp_path)
    report = prepare_vivado_inputs(source, tmp_path, RUNTIME, contract)
    fixed = report['fixed_io']
    assert report['instances'] == 2 and report['dut_clock_count'] == 1
    assert report['top'] == '__emuflow_physical_top'
    assert Path(fixed['query']).read_text() == (
        'direction\tp_hex\tn_hex\tp_pin_hex\tn_pin_hex\tiostandard_hex\n' +
        '\t'.join(['tx', *(value.encode().hex() for value in ['link_p','link_n','P1','P2','LVDS'])])+'\n')
    assert Path(fixed['timing_map']).read_text() == (
        'logical_hex\tphysical_hex\n'+ 'q[0]'.encode().hex()+'\t'+'link_p'.encode().hex()+'\n'+
        'q'.encode().hex()+'\t'+'link_p'.encode().hex()+'\n')
    top = Path(fixed['wrapper']).read_text()
    assert 'wire [0:0] \\q ;' in top
    assert 'output wire \\q ' not in top
    assert top.count('.\\q (\\q )') == 2
    for name in ('link_p','link_n'):
        assert 'output wire \\'+name+' ' in top
        assert top.count('.\\'+name+' (\\'+name+' )') == 1
    assert not list(tmp_path.glob('.vivado-input*'))


@pytest.mark.parametrize('damage', ['stale','direction','width','bit','duplicate-bit',
    'physical-name','logical-collision','physical-count','pair-count','fabric-clock','dut-clock',
    'clock-period','clock-injection','generation','source-format','nonfinite'])
def test_native_inputs_reject_damage_before_publishing_any_generation(tmp_path, damage):
    source = fixture(tmp_path)
    contract, runtime = fixed_contract(tmp_path), copy.deepcopy(RUNTIME)
    entry = contract['entries'][0]
    generation = list(native_file_generation(source))
    if damage == 'stale': (tmp_path/'pins.xdc').write_text('changed')
    elif damage == 'direction': entry['direction'] = 'rx'
    elif damage == 'width': entry['logical_port_width'] = 2
    elif damage == 'bit': entry['logical_bit'] = 1
    elif damage == 'duplicate-bit': contract['entries'].append(copy.deepcopy(entry))
    elif damage == 'physical-name': entry['n']['port'] = entry['p']['port']
    elif damage == 'logical-collision': entry['p']['port'] = 'd'
    elif damage == 'physical-count': contract['metrics']['physical_ports'] = 4
    elif damage == 'pair-count': contract['metrics']['differential_pairs'] = 2
    elif damage == 'fabric-clock': runtime['timing_model']['fabric_clock_port'] = 'missing'
    elif damage == 'dut-clock': runtime['timing_model']['dut_clock_ports'] = ['clk','clk']
    elif damage == 'clock-period': runtime['fabric_clock']['period_ns'] = -1
    elif damage == 'clock-injection': runtime['timing_model']['fabric_clock_port'] = 'fabric_clk}];exec'
    elif damage == 'generation': generation[2] += 1
    elif damage == 'source-format':
        source = tmp_path/'whole.json'; source.write_text('{}'); generation = list(native_file_generation(source))
    else: runtime['fabric_clock']['period_ns'] = float('inf')
    request = tmp_path/'request.json'
    request.write_text(json.dumps({'schema':'emuflow.vivado-input-request/v1',
        'source':{'path':str(source),'generation':generation},'runtime':runtime,'virtual_io_contract':contract}))
    destination = tmp_path/'generation'
    result = subprocess.run([str(emuflow_native()),'physical','prepare-vivado-inputs',str(request),str(destination)],
                            capture_output=True,text=True)
    assert result.returncode != 0
    assert not destination.exists()
    assert not list(tmp_path.glob('.vivado-inputs-*'))


def test_native_inputs_do_not_replace_a_published_generation(tmp_path):
    source = fixture(tmp_path)
    report = prepare_vivado_inputs(source, tmp_path, RUNTIME)
    destination = Path(report['runtime_xdc']).parent
    before = (destination/'summary.json').read_bytes()
    request = tmp_path/'request.json'
    request.write_text(json.dumps({'schema':'emuflow.vivado-input-request/v1',
        'source':{'path':str(source),'generation':native_file_generation(source)},
        'runtime':RUNTIME,'virtual_io_contract':None}))
    result = subprocess.run([str(emuflow_native()),'physical','prepare-vivado-inputs',str(request),str(destination)],
                            capture_output=True,text=True)
    assert result.returncode != 0
    assert (destination/'summary.json').read_bytes() == before
