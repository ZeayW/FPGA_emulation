import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.indexed_timing import IndexedTimingView
from emuflow.opensta import DEFAULT_TIMING_MODEL, load_timing_model, render_opensta_liberty
from emuflow.sta_clock_logic import lut_clock_model, specialize_clock_logic
from emuflow.yosys import import_yosys_json


@pytest.mark.parametrize('init,expected',[
    ('1000', {'I0':'positive_unate','I1':'positive_unate'}),
    ('0111', {'I0':'negative_unate','I1':'negative_unate'}),
    ('0110', {'I0':'non_unate','I1':'non_unate'}),
    ('1010', {'I0':'positive_unate'}),
    ('0000', {}),
])
def test_truth_table_arc_polarity_and_unused_inputs(init,expected):
    model=load_timing_model(DEFAULT_TIMING_MODEL)
    name,gate=lut_clock_model({'type':'LUT2','parameters':{'INIT':init}},model['cells']['LUT2'])
    assert gate['timing_sense']=={'O':expected}
    library=render_opensta_liberty({**model,'cells':{name:gate}})
    for pin in ('I0','I1'):
        assert (f'related_pin : "{pin}";' in library)==(pin in expected)
    assert 'timing_sense' not in model['cells']['LUT2']


def test_dual_output_lut_keeps_distinct_truth_tables():
    _name,gate=lut_clock_model({'type':'LUT6_2','parameters':{'INIT':0xffffffff00000000}}, {})
    assert gate['timing_sense']=={'O5':{},'O6':{'I5':'positive_unate'}}


@pytest.mark.parametrize('constant,sense',[('0','positive_unate'),('1','negative_unate')])
def test_tied_xor_input_selects_its_actual_polarity(constant,sense):
    name,gate=lut_clock_model({'type':'LUT2','parameters':{'INIT':'0110'},
        'constant_connections':[{'port':'I1','bit':0,'value':constant}]}, {})
    assert gate['timing_sense']=={'O':{'I0':sense}}
    assert '_CONST_2_'+str(int(constant) << 1) in name


def test_conflicting_or_unknown_clock_constants_fail():
    for values in [('0','1'),('x',)]:
        with pytest.raises(ValidationError):
            lut_clock_model({'type':'LUT2','parameters':{'INIT':'0110'},
                'constant_connections':[{'port':'I1','bit':0,'value':v}for v in values]}, {})


@pytest.mark.parametrize('init',[True,-1,16,'10x0','',None])
def test_unknown_or_overwidth_init_cannot_choose_a_clock_edge(init):
    with pytest.raises(ValidationError):
        lut_clock_model({'type':'LUT2','parameters':{'INIT':init}}, {})


@pytest.mark.parametrize('constant_kind',['literal','vcc'])
def test_native_clock_cone_specialization_does_not_change_data_luts_or_source(tmp_path,constant_kind):
    cells={}
    def cell(name,kind,connections,directions,parameters=None):
        cells[name]={'type':kind,'parameters':parameters or {},'attributes':{},
            'connections':connections,'port_directions':directions}
    lp={'I0':'input','I1':'input','O':'output'}
    cell('gate','LUT2',{'I0':[2],'I1':[3],'O':[4]},lp,{'INIT':'1000'})
    cell('inverter','LUT1',{'I0':[4],'O':[5]},{'I0':'input','O':'output'},{'INIT':'01'})
    if constant_kind=='vcc':cell('one','VCC',{'P':[10]},{'P':'output'})
    cell('clock_xor','LUT2',{'I0':[5],'I1':['1']if constant_kind=='literal'else[10],'O':[8]},lp,{'INIT':'0110'})
    cell('data','LUT2',{'I0':[3],'I1':[6],'O':[7]},lp,{'INIT':'0110'})
    cell('ff','FDRE',{'C':[8],'D':[7],'Q':[6],'CE':['1'],'R':['0']},
        {'C':'input','D':'input','Q':'output','CE':'input','R':'input'})
    raw={'modules':{'top':{'attributes':{'top':'1'},'ports':{
        'clk':{'direction':'input','bits':[2]},'enable':{'direction':'input','bits':[3]},
        'q':{'direction':'output','bits':[6]}},'cells':cells,'netnames':{}}}}
    source=tmp_path/'source.json';source.write_text(json.dumps(raw))
    ir=import_yosys_json(source,clocks=['clk']);path=tmp_path/'source.emuir.sqlite';ir.save(path)
    before=path.read_bytes()
    view=IndexedTimingView(path)
    try:
        base=load_timing_model(DEFAULT_TIMING_MODEL);model=deepcopy(base)
        with patch('emuflow.ir.EmuIR.load',side_effect=AssertionError('whole graph forbidden')):
            summary=specialize_clock_logic(view,model)
            assert summary['lut_instances']==3 and summary['distinct_lut_models']==3
            types=view.timing_types()
            assert types['gate']=='LUT2__CLOCK_INIT_8'
            assert types['inverter']=='LUT1__CLOCK_INIT_1'
            assert types['clock_xor']=='LUT2__CLOCK_INIT_6_CONST_2_2'
            assert model['cells'][types['clock_xor']]['timing_sense']=={'O':{'I0':'negative_unate'}}
            assert types['data']=='LUT2'
            assert model['cells']['LUT2']==base['cells']['LUT2']
            verilog='\n'.join(view.timing_verilog_lines(types))
            assert 'LUT2__CLOCK_INIT_8' in verilog and 'LUT1__CLOCK_INIT_1' in verilog
            expected={'FDRE','LUT2','LUT2__CLOCK_INIT_8','LUT1__CLOCK_INIT_1','LUT2__CLOCK_INIT_6_CONST_2_2'}
            if constant_kind=='vcc':expected.add('VCC')
            assert set(view.used_timing_types(types))==expected
        assert path.read_bytes()==before
    finally:view.close()


def test_explicit_arc_schema_validation(tmp_path):
    value=load_timing_model(DEFAULT_TIMING_MODEL)
    value['cells']['LUT1']['timing_sense']={'O':{'I2':'positive_unate'}}
    path=tmp_path/'bad.json';path.write_text(json.dumps(value))
    with pytest.raises(ValidationError,match='arc polarity'):load_timing_model(path)
