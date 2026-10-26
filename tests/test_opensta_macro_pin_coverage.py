"""Later macro occurrences must not lose pins or timing endpoints."""
import json
import os
import tracemalloc
from copy import deepcopy
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.indexed_timing import IndexedTimingView
from emuflow.opensta import (build_architecture_opensta_timing_model,
    _timing_model_pin_directions, run_opensta_path_database)
from emuflow.sta import iter_sta_path_database_paths
from emuflow.verilog import write_mapped_verilog
from emuflow.yosys import import_yosys_json
from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract


@pytest.fixture
def macros(tmp_path):
    cells={
        'a_narrow':dict(type='RAMB18E2',parameters={},
            port_directions=dict(CLKARDCLK='input',ADDR='input',DOUT='output'),
            connections=dict(CLKARDCLK=[2],ADDR=[3],DOUT=[4])),
        'z_wide':dict(type='RAMB18E2',parameters={},
            port_directions=dict(CLKARDCLK='input',ADDR='input',DINADIN='input',
                RSTREGARSTREG='input',DOUT='output'),
            connections=dict(CLKARDCLK=[2],ADDR=[3]*8,DINADIN=['0']*8,
                RSTREGARSTREG=['0'],DOUT=list(range(100,164)))),
    }
    for i,data in enumerate((4,163)):
        cells[f'capture{i}']=dict(type='FDRE',parameters={},
            port_directions=dict(C='input',CE='input',D='input',R='input',Q='output'),
            connections=dict(C=[2],CE=['1'],D=[data],R=['0'],Q=[200+i]))
    value=dict(modules={'top':dict(attributes={'top':'1'},cells=cells,
        ports=dict(clk=dict(direction='input',bits=[2]),addr=dict(direction='input',bits=[3]),
            q=dict(direction='output',bits=[200,201])),netnames={})})
    yosys=tmp_path/'mapped.json';yosys.write_text(json.dumps(value))
    ir=import_yosys_json(yosys,top='top',clocks=['clk'])
    source=tmp_path/'source.emuir.sqlite';ir.save(source)
    architecture=write_mock_xilinx_timing_contract(tmp_path/'architecture.json')
    return ir,source,architecture


def test_model_covers_later_wide_macro_constants_and_both_scalar_spellings(macros,tmp_path):
    ir,source,architecture=macros
    expected,types=build_architecture_opensta_timing_model(ir,architecture)
    bank=expected['cells']['RAMB18E2']
    assert {'DOUT','DOUT__0','DOUT__63'} <= set(bank['outputs'])
    assert {'ADDR','ADDR__7','DINADIN__7'} <= set(bank['inputs'])
    assert 'RSTREGARSTREG' in bank['controls']
    with patch('emuflow.ir.EmuIR.load',side_effect=AssertionError('whole graph reload')):
        view=IndexedTimingView(source)
        try:
            model,indexed_types=build_architecture_opensta_timing_model(view,architecture)
            assert model==expected
            write_mapped_verilog(view,tmp_path/'indexed.v',timing_only=True,
                timing_cell_types=indexed_types,timing_model_pins=_timing_model_pin_directions(model))
        finally:view.close()
    write_mapped_verilog(ir,tmp_path/'reference.v',timing_only=True,
        timing_cell_types=types,timing_model_pins=_timing_model_pin_directions(expected))
    assert (tmp_path/'indexed.v').read_bytes()==(tmp_path/'reference.v').read_bytes()


@pytest.mark.parametrize('indexed',[False,True])
@pytest.mark.parametrize('damage',['missing-output','wrong-direction','missing-constant'])
def test_emission_rejects_missing_or_misdirected_pins_before_publication(macros,tmp_path,indexed,damage):
    ir,source,architecture=macros
    model,types=build_architecture_opensta_timing_model(ir,architecture)
    contract=_timing_model_pin_directions(model)
    pin='DINADIN__7' if damage=='missing-constant' else 'DOUT__63'
    if damage=='wrong-direction':contract['RAMB18E2'][pin]=1
    else:del contract['RAMB18E2'][pin]
    output=tmp_path/'kept.v';output.write_text('previous valid publication')
    view=IndexedTimingView(source) if indexed else ir
    try:
        with pytest.raises(ValidationError,match='model pin is missing or has wrong direction'):
            write_mapped_verilog(view,output,timing_only=True,timing_cell_types=types,timing_model_pins=contract)
        assert output.read_text()=='previous valid publication'
        assert not list(tmp_path.glob('.kept.v.*.tmp'))
    finally:
        if indexed:view.close()


def test_type_pin_catalog_is_bounded_by_pins_not_macro_population(macros,tmp_path):
    ir,_,_=macros
    original=next(cell for cell in ir.value['instances'] if cell['id']=='z_wide')
    templates=[(net,role,endpoint) for net in ir.value['nets'] for role in ['drivers','sinks']
        for endpoint in net[role] if endpoint['instance']==original['id']]
    for i in range(600):
        cell=deepcopy(original);cell['id']=cell['name']=f'repeat{i}'
        ir.value['instances'].append(cell)
        for net,role,endpoint in templates:
            copied={**endpoint,'instance':cell['id']}
            if role=='sinks':net[role].append(copied)
            else:
                name=f'repeat{i}_{endpoint["port"]}_{endpoint["bit"]}'
                ir.value['nets'].append(dict(id=name,name=name,aliases=[],cut_class='register_output',
                    drivers=[copied],sinks=[],fanout=0))
    for net in ir.value['nets']:net['fanout']=len(net['sinks'])
    source=tmp_path/'repeated.emuir.sqlite';ir.save(source)
    view=IndexedTimingView(source)
    try:
        tracemalloc.start()
        catalog=view.timing_type_pin_names({'RAMB18E2'})
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        assert set(catalog)=={'RAMB18E2'}
        assert len(catalog['RAMB18E2']['outputs'])==65
        assert peak<3*1024*1024
    finally:view.close()


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),reason='explicit qualified OpenSTA required')
def test_real_engine_times_late_wide_ram_high_bit_without_missing_ports(macros,tmp_path):
    _,source,architecture=macros
    output=tmp_path/'paths.json';log=tmp_path/'opensta.log'
    result=run_opensta_path_database(source,output,clocks={'clk':10.},
        architecture_timing_db_path=architecture,
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],log_path=log,
        _stream_output=True,clock_budget_policy='engine-edge-v1')
    assert result['status']=='pass' and 'Warning 201:' not in log.read_text()
    paths=list(iter_sta_path_database_paths(output,json.loads(output.read_text())))
    assert any(row['startpoint']['instance']=='z_wide' and row['startpoint']['port']=='DOUT'
        and row['startpoint']['bit']==63 and row['endpoint']['instance']=='capture1' for row in paths)
