"""Port resizing cannot discard a live wire or modify a retained pin."""
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.mapped_normalization import validate_mapped_normalization, load_mapped_normalization_proof
from emuflow.yosys import import_yosys_json
from tests.native_build import emuflow_native


def source(tmp_path, name, value):
    path = tmp_path/(name+'.json')
    path.write_text(json.dumps(value))
    ir = tmp_path/(name+'.emuir.sqlite')
    import_yosys_json(path, top='top', clocks=['clk']).save(ir)
    return ir


def fixture(tmp_path):
    cell = dict(type='RAMB18E2', parameters={'INIT_00': '0'*256, 'READ_WIDTH_A': '0'*27+'10000'},
        port_directions={'CLKARDCLK':'input','WEA':'input','DOUTADOUT':'output','DINADIN':'input'},
        connections={'CLKARDCLK':[2], 'WEA':[3,3,3,3], 'DOUTADOUT':list(range(10,42)), 'DINADIN':['0']*32})
    before = dict(modules={'top':dict(cells={'ram':cell}, ports={
        'clk':dict(direction='input',bits=[2]),'enable':dict(direction='input',bits=[3]),
        'q':dict(direction='output',bits=[10])},
        netnames={f'wire{i}':dict(bits=[i],hide_name=0,attributes={}) for i in [2,3,*range(10,42)]})})
    after = deepcopy(before)
    con = after['modules']['top']['cells']['ram']['connections']
    con['DOUTADOUT'] = con['DOUTADOUT'][:16]
    con['DINADIN'] = con['DINADIN'][:16]
    con['WEA'] = con['WEA'][:2]
    library = tmp_path/'cells_xtra.v'
    library.write_text(''.join('module '+name+' ();\ninput CLKARDCLK;\ninput [1:0] WEA;\n'
        'input [15:0] DINADIN;\noutput [15:0] DOUTADOUT;\nendmodule\n'
        for name in ['RAMB18E2','RAMB36E2','URAM288']))
    return before,after,library


def validate(tmp_path,before,after,library):
    old,new = source(tmp_path,'old',before),source(tmp_path,'new',after)
    # The production adapter must never reconstruct a graph in Python.
    with patch('emuflow.ir.EmuIR.load',side_effect=AssertionError('whole graph reload')):
        return validate_mapped_normalization(old,new,library,tmp_path/'proof.json',executable=str(emuflow_native()))


def test_proves_all_real_pins_and_only_removes_unused_nonphysical_padding(tmp_path):
    before,after,library = fixture(tmp_path)
    report = validate(tmp_path,before,after,library)
    assert report['cells']==1 and report['removed_nonphysical_pins']==34
    assert report['retained_pins']>30


@pytest.mark.parametrize('damage',['parameter','real-wire','constant','live-padding','write-enable','missing-pin','added-cell','unknown-port','added-real-pin','new-port'])
def test_rejects_each_unproved_change_without_replacing_previous_proof(tmp_path,damage):
    before,after,library = fixture(tmp_path)
    b = before['modules']['top']; a = after['modules']['top'];cell = a['cells']['ram']
    if damage=='parameter':cell['parameters']['INIT_00']='1'+'0'*255
    elif damage=='real-wire':cell['connections']['CLKARDCLK']=[3]
    elif damage=='constant':cell['connections']['DINADIN'][0]='1'
    elif damage=='live-padding':b['ports']['q']['bits']=[41]
    elif damage=='write-enable':b['cells']['ram']['connections']['WEA'][3]=2
    elif damage=='missing-pin':del cell['connections']['CLKARDCLK']
    elif damage=='added-cell':a['cells']['extra']=deepcopy(cell)
    elif damage=='added-real-pin':
        del b['cells']['ram']['connections']['CLKARDCLK']
    elif damage=='new-port':
        a['ports']['surprise']=dict(direction='input',bits=[5])
    else:
        b['cells']['ram']['connections']['UNKNOWN']=['0']
        b['cells']['ram']['port_directions']['UNKNOWN']='input'
        cell['connections']['UNKNOWN']=['0'];cell['port_directions']['UNKNOWN']='input'
    output=tmp_path/'proof.json';output.write_bytes(b'previous checked result')
    with pytest.raises(ValidationError,match='proof failed'):
        validate(tmp_path,before,after,library)
    assert output.read_bytes()==b'previous checked result'


def test_zero_extension_and_sinkless_physical_outputs_are_checked(tmp_path):
    before,after,library=fixture(tmp_path)
    b=before['modules']['top']['cells']['ram']['connections']
    a=after['modules']['top']['cells']['ram']['connections']
    b['DINADIN']=b['DINADIN'][:8];b['DOUTADOUT']=b['DOUTADOUT'][:8]
    assert validate(tmp_path,before,after,library)['status']=='pass'
    a['DINADIN'][15]='1'
    with pytest.raises(ValidationError,match='added an unproved pin'):
        validate(tmp_path,before,after,library)


def test_net_aliases_survive_renumbering_without_changing_pin_connectivity(tmp_path):
    before,after,library=fixture(tmp_path)
    # Provider bit numbers and preferred wire names are not electrical
    # identities. Retain every original wire name as an explicit alias.
    module=after['modules']['top']
    for record in [*module['ports'].values(),*module['netnames'].values(),
                   *[dict(bits=bits) for bits in module['cells']['ram']['connections'].values()]]:
        record['bits'][:]=[v+1000 if isinstance(v,int) else v for v in record['bits']]
    module['netnames'].update({f'aaa_preferred_{i}':deepcopy(record)
        for i,record in enumerate(list(module['netnames'].values()))})
    assert validate(tmp_path,before,after,library)['status']=='pass'


def bound_proof(tmp_path):
    before, after, library = fixture(tmp_path)
    report = validate(tmp_path, before, after, library)
    parents = {role:record for role,record in report['source_artifacts'].items()}
    arguments = {role:parents[role]['path'] for role in parents}
    arguments.update({role+'_sha256':parents[role]['sha256'] for role in parents})
    return tmp_path/'proof.json', report, arguments


def test_normalization_admission_binds_all_sources_without_rehashing_graphs(tmp_path, monkeypatch):
    path, report, arguments = bound_proof(tmp_path)
    for record in report['source_artifacts'].values():
        assert record['sha256'] == file_sha256(Path(record['path']))
        assert record['bytes'] > 0 and len(record['generation']) == 7
    def only_proof_digest(candidate):
        assert candidate == path, 'proof admission rehashed an entire parent graph'
        return file_sha256(candidate)
    monkeypatch.setattr('emuflow.mapped_normalization.file_sha256', only_proof_digest)
    owner = load_mapped_normalization_proof(path, **arguments)
    assert owner.cells == report['cells'] and owner.removed_nonphysical_pins == 34
    assert owner.artifact_sha256 == file_sha256(path)
    owner.require_unchanged()


@pytest.mark.parametrize('damage', ['counts-only', 'wrong-seal', 'another-source', 'changed-source', 'changed-library'])
def test_normalization_proof_cannot_admit_unrelated_or_changed_source_views(tmp_path, damage):
    path, report, arguments = bound_proof(tmp_path)
    if damage == 'counts-only':
        del report['source_artifacts']
        path.write_text(json.dumps(report))
    elif damage == 'wrong-seal':
        arguments['normalized_sha256'] = '0'*64
    elif damage == 'another-source':
        original = tmp_path/'old.emuir.sqlite'
        copied = tmp_path/'copied.emuir.sqlite'; copied.write_bytes(original.read_bytes())
        arguments['original'] = str(copied)
    elif damage == 'changed-source':
        source = tmp_path/'new.emuir.sqlite'
        with source.open('ab') as stream:
            stream.write(b'changed-generation')
    else:
        library = tmp_path/'cells_xtra.v'; library.write_text(library.read_text()+'\n')
    with pytest.raises(ValidationError, match='source-bound|parent seal or generation'):
        load_mapped_normalization_proof(path, **arguments)


def test_admitted_proof_retains_parent_generation_checks(tmp_path):
    path, _, arguments = bound_proof(tmp_path)
    owner = load_mapped_normalization_proof(path, **arguments)
    with (tmp_path/'new.emuir.sqlite').open('ab') as stream:
        stream.write(b'new source generation')
    with pytest.raises(ValidationError, match='source generation changed'):
        owner.require_unchanged()
