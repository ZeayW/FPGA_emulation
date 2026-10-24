"""Independent complete TSV parity and rejection tests for shared RWRoute export."""
import copy
import json
import os
import random
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
import unittest
import zlib
from contextlib import closing
from unittest import mock

from emuflow import rwroute_native
from emuflow.errors import ValidationError
from emuflow.xilinx_netlist import open_xilinx_mapped_index
from emuflow.xilinx_physical_handoff import open_xilinx_physical_handoff, write_xilinx_physical_handoff
from emuflow.xilinx_rwroute import export_rwroute_input
from tests.native_build import emuflow_native

DIGESTS = {'mapped_sha256': '1'*64, 'packed_sha256': '2'*64, 'placement_sha256': '2'*64}


def fixture(root):
    def cell(kind, connections, outputs=(), parameters=None):
        return {'type': kind, 'parameters': parameters or {}, 'attributes': {},
                'connections': connections,
                'port_directions': {p: 'output' if p in outputs else 'input' for p in connections}}
    cells = {
        'z_ff': cell('FDRE', {'C': [91], 'D': [2], 'Q': [8]}, ['Q']),
        'enable': cell('FDRE', {'C': [1], 'Q': [90]}, ['Q']),
        'clock': cell('LUT2', {'I0': [1], 'I1': [90], 'O': [91]}, ['O'], {'INIT': '1000'}),
        'a_lut': cell('LUT1', {'I0': [100], 'O': [2]}, ['O']),
        'adapter': cell('LUT6_2', {'I0': [2], 'I1': [8], 'I2': ['0'], 'O5': [4], 'O6': [5]}, ['O5', 'O6']),
        'dsp': cell('DSP48E2', {'A': [4, 5, '0'], 'P': [6]}, ['P']),
        'ram': cell('RAMB36E2', {'DIADI': [6, 40, 57], 'DOADO': [7]}, ['DOADO'], {
            'READ_WIDTH_A': '00000000000000100100', 'READ_WIDTH_B': 18,
            'WRITE_WIDTH_A': '0', 'WRITE_WIDTH_B': '1'*80,
            'DOA_REG': True, 'DOB_REG': False, 'WRITE_MODE_A': 'NO_CHANGE',
            'WRITE_MODE_B': 'READ_FIRST', 'RAM_MODE': 'SDP', 'INIT_00': '1'*2048}),
        'carry': cell('CARRY8', {'CI': [7], 'CO': list(range(40,48)), 'O': list(range(50,58))}, ['CO','O']),
        '中文': cell('LUT1', {'I0': [101], 'O': [102]}, ['O']),
        'ground': cell('GND', {'G': [999]}, ['G']),
    }
    mapped = {'modules': {'top': {'cells': cells, 'ports': {
        'tx': {'direction': 'output', 'bits': [4]},
        'rx': {'direction': 'input', 'bits': [100]},
    }, 'netnames': {}}}}
    packed = {'schema': 'emuflow.packed-site-netlist/v1', 'status': 'pass',
              'mapping_profile': 'fixture', 'top': 'top', 'source': {}, 'policy': {},
              'clusters': [], 'cascade_chains': [], 'unplaced_constants': ['ground'], 'summary': {}}
    placement = {'schema': 'emuflow.xilinx-placement/v1', 'status': 'pass',
                 'part': 'xcvu19p-test', 'provider': 'fixture', 'source': {}, 'policy': {},
                 'clusters': [], 'summary': {}}
    for index, (name, value) in enumerate(cells.items()):
        if value['type'] == 'GND':
            continue
        bel = 'A6LUT' if value['type'].startswith('LUT') else 'AFF'
        site = f'SLICE_X0Y{index}'
        assignment = {'instance': name, 'cell_type': value['type'], 'bel': bel}
        packed['clusters'].append({'id': name, 'kind': 'slice', 'site_templates': ['SLICEL'],
                                   'control_set': None, 'assignments': [assignment]})
        placement['clusters'].append({'cluster': name, 'site': site, 'site_type': 'SLICEL',
            'x': 0, 'y': index, 'fixed': False, 'physical_region': None,
            'assignments': [{'instance': name, 'cell_type': value['type'], 'bel': bel, 'placement_mode': 'fixed', 'site': site}]})
    contract = {'schema': 'emuflow.virtual-io-physical-contract/v2', 'status': 'pass', 'entries': []}
    for i, direction in enumerate(('tx', 'rx')):
        contract['entries'].append({'id': 'io'+str(i), 'direction': direction,
            'logical_port': direction, 'logical_bit': 0, 'logical_port_width': 1, 'iostandard': 'LVDS',
            'p': {'port': f'io{i}_p', 'package_pin': f'P{i}', 'site': f'IOB_X0Y{2*i}'},
            'n': {'port': f'io{i}_n', 'package_pin': f'N{i}', 'site': f'IOB_X0Y{2*i+1}'}})
    mapped_path, handoff = root/'mapped ?#%中文.sqlite', root/'physical ?#%中文.sqlite'
    write_mapped(mapped_path, mapped)
    write_xilinx_physical_handoff(handoff, packed, placement)
    return mapped, packed, placement, contract, mapped_path, handoff


def write_mapped(path, mapped):
    module = mapped['modules']['top']
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE cells(id TEXT PRIMARY KEY,seq INTEGER UNIQUE,type TEXT,record_json TEXT);
        CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port TEXT,port_bit INTEGER,
          port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER,constant TEXT,
          PRIMARY KEY(cell_id,port,port_bit)) WITHOUT ROWID;
        CREATE TABLE netnames(id TEXT PRIMARY KEY,seq INTEGER,record_json TEXT);
        CREATE INDEX pins_net ON cell_pins(net_bit,cell_id,port,port_bit);
        ''')
        db.executemany('INSERT INTO metadata VALUES (?,?)', [
            ('schema', 'emuflow.xilinx-mapped-index/v2'), ('top_json', '"top"'),
            ('creator_json', '"fixture"'), ('ports_json', json.dumps(module['ports'])),
        ])
        for seq, (name, value) in enumerate(module['cells'].items()):
            db.execute('INSERT INTO cells VALUES (?,?,?,?)', (name, seq, value['type'], json.dumps({
                k: v for k, v in value.items() if k not in {'connections', 'port_directions'}})))
            for port, values in value['connections'].items():
                for index, bit in enumerate(values):
                    net = isinstance(bit, int)
                    db.execute('INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?,?)', (
                        seq, name, port, index, len(values),
                        {'input': 0, 'output': 1, 'inout': 2}[value['port_directions'][port]],
                        0 if net else 1, bit if net else None, None if net else bit))


def native(root, values, *, contract=True):
    mapped, packed, placement, io, mapped_path, handoff = values
    packed_view, placement_view = open_xilinx_physical_handoff(handoff)
    with open_xilinx_mapped_index(mapped_path) as indexed, mock.patch.object(
            rwroute_native, 'resolve_native_executable', return_value=str(emuflow_native())):
        with mock.patch.object(indexed, 'iter_physical_cells',
                side_effect=AssertionError('Python cell cursor')), mock.patch.object(
                indexed, 'iter_physical_route_endpoints_by_net',
                side_effect=AssertionError('Python pin cursor')), mock.patch.object(
                indexed, 'iter_qualified_lut_clock_gates',
                side_effect=AssertionError('Python clock qualification')), mock.patch.object(
                type(indexed.cells), '__getitem__', side_effect=AssertionError('Python cell lookup')):
            return export_rwroute_input(mapped_path, handoff, handoff, root/'native.tsv',
                mapped_value=indexed, packed_value=packed_view, placement_value=placement_view,
                source_sha256=DIGESTS, virtual_io_contract=io if contract else None,
                resource_limits={'lut': 100, 'ff': 200, 'dsp48': 3})


class NativeRwrouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = emuflow_native()

    def test_complete_reference_tsv_parity_and_no_python_data_plane(self):
        for use_io in (False, True):
            with self.subTest(io=use_io), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); values=fixture(root)
                mapped,packed,placement,io,mp,handoff=values
                expected=export_rwroute_input(root/'mapped.json',root/'packed.json',root/'placed.json',root/'reference.tsv',
                    mapped_value=mapped,packed_value=packed,placement_value=placement,
                    source_sha256=DIGESTS,virtual_io_contract=io if use_io else None,
                    resource_limits={'lut': 100,'ff': 200,'dsp48': 3})
                before={p:p.read_bytes() for p in (mp,handoff)}
                actual=native(root,values,contract=use_io)
                self.assertEqual((root/'native.tsv').read_bytes(),(root/'reference.tsv').read_bytes())
                for key in ('cells','logical_cells','physical_cells','virtual_io_cells','expanded_lut6_2_cells',
                            'transformed_dsp48e2_cells','qualified_clock_gates','routable_nets','excluded_nets'):
                    self.assertEqual(actual[key],expected[key],key)
                self.assertEqual(actual['schema'],'emuflow.rwroute-export-summary/v1')
                self.assertEqual(actual['implementation_id'],'native-rwroute-export-v2')
                self.assertEqual(actual['qualified_clock_gates'],1)
                self.assertGreater(actual['runtime_breakdown']['maximum_live_net_endpoints'],1)
                self.assertEqual(actual['runtime_breakdown']['source_cell_catalog_bytes'],16*len(mapped['modules']['top']['cells']))
                self.assertLessEqual(actual['runtime_breakdown']['clock_pin_rows_visited'],2*actual['runtime_breakdown']['source_pin_rows_visited'])
                self.assertEqual({p:p.read_bytes() for p in before},before)
                self.assertFalse(list(root.glob('.emuflow-rwroute-*')))
                self.assertFalse(list(root.glob('*.tmp.*')))
                text=(root/'native.tsv').read_text()
                self.assertNotIn('INIT_00',text)
                self.assertIn(str((1<<80)-1),text)

    def test_integer_parameter_tokens_do_not_round_through_sqlite_real(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); values=fixture(root)
            mapped,packed,placement,io,mp,handoff=values
            parameters=mapped['modules']['top']['cells']['ram']['parameters']
            parameters['READ_WIDTH_A']=10**60+123
            parameters['READ_WIDTH_B']=-(10**55+456)
            with closing(sqlite3.connect(mp)) as db,db:
                record=json.loads(db.execute("SELECT record_json FROM cells WHERE id='ram'").fetchone()[0])
                record['parameters']=parameters
                db.execute("UPDATE cells SET record_json=? WHERE id='ram'",(json.dumps(record),))
            native(root,values)
            text=(root/'native.tsv').read_text()
            self.assertIn(str(parameters['READ_WIDTH_A']),text)
            self.assertIn(str(parameters['READ_WIDTH_B']),text)

    def test_invalid_source_relations_preserve_prior_output(self):
        mutations={
            'type': "UPDATE cells SET type='LUT3' WHERE id='a_lut'",
            'source_gap': "UPDATE cells SET seq=seq+1000 WHERE id='z_ff'",
            'pin_owner': "UPDATE cell_pins SET cell_seq=(SELECT seq FROM cells WHERE id='a_lut') WHERE cell_id='z_ff' AND port='D'",
            'orphan_seq': "UPDATE cell_pins SET cell_seq=1000000 WHERE cell_id='z_ff' AND port='D'",
            'orphan_label': "UPDATE cell_pins SET cell_id='missing' WHERE cell_id='z_ff' AND port='D'",
            'schema': "UPDATE metadata SET value='emuflow.xilinx-mapped-index/v99' WHERE key='schema'",
            'direction': "UPDATE cell_pins SET direction=4 WHERE cell_id='z_ff' AND port='D'",
            'width': "UPDATE cell_pins SET port_width=0 WHERE cell_id='z_ff' AND port='D'",
            'unsupported_lut_pin': "UPDATE cell_pins SET value_kind=0,net_bit=8,constant=NULL WHERE cell_id='adapter' AND port='I2'",
            'bad_gate': "UPDATE cells SET record_json='{}' WHERE id='clock'",
            'ram_projection': "UPDATE cells SET record_json='{\"type\":\"LUT3\"}' WHERE id='ram'",
            'bad_record': "UPDATE cells SET record_json='[]' WHERE id='clock'",
            'carry_conflict': "UPDATE cell_pins SET net_bit=50 WHERE cell_id='ram' AND port_bit=2",
            'mixed_mapped_source': "UPDATE physical.document SET value_json=json_set(value_json,'$.source.mapped_json_sha256','wrong') WHERE name='packed'",
            'tab_identity': "UPDATE physical.assignment SET physical_site='bad'||char(9)||'site' WHERE instance='a_lut'",
        }
        for label,mutation in mutations.items():
            with self.subTest(label=label),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);values=fixture(root)
                mp,handoff=values[-2:]
                with closing(sqlite3.connect(mp)) as db,db:
                    db.execute('ATTACH DATABASE ? AS physical',(str(handoff),))
                    db.execute(mutation)
                (root/'native.tsv').write_text('old publication\n')
                with self.assertRaises(ValidationError):native(root,values)
                self.assertEqual((root/'native.tsv').read_text(),'old publication\n')
                self.assertFalse(list(root.glob('.emuflow-rwroute-*')))
                self.assertFalse(list(root.glob('*.tmp.*')))

    def test_bad_fixed_io_binding_is_rejected_without_publication(self):
        for mutation in ('direction','width','duplicate','bit'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);values=fixture(root);io=values[3]
                if mutation=='direction':io['entries'][0]['direction']='rx'
                elif mutation=='width':io['entries'][0]['logical_port_width']=2
                elif mutation=='bit':io['entries'][0]['logical_bit']=True
                else:io['entries'].append(copy.deepcopy(io['entries'][0]))
                with self.assertRaises(ValidationError):native(root,values)
                self.assertFalse((root/'native.tsv').exists())

    def test_missing_unified_runtime_does_not_fall_back_to_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);values=fixture(root)
            with mock.patch.object(rwroute_native, 'export_native_rwroute', side_effect=ValidationError('required native owner')):
                # Patch the actual imported entry in the production selector.
                with self.assertRaisesRegex(ValidationError,'required native owner'):
                    native(root,values)
            self.assertFalse((root/'native.tsv').exists())

    def test_request_corruption_generation_and_aliases_fail_closed(self):
        original=rwroute_native.NativeToolProcess
        for mutation in ('crc','version','truncated','trailing','generation','source_changed',
                         'output_input_alias','summary_input_alias','summary_output_alias','report_input_alias'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);values=fixture(root)
                mapped_path,handoff=values[-2:]
                before={path:path.read_bytes() for path in (mapped_path,handoff)}
                output=root/'native.tsv';output.write_text('prior publication\n')
                def launch(runner,command,**kwargs):
                    command=list(command);request=Path(command[7])
                    data=bytearray(request.read_bytes())
                    if mutation=='crc':data[-5]^=1
                    elif mutation=='version':struct.pack_into('<I',data,8,99)
                    elif mutation=='truncated':data=data[:-5]
                    elif mutation=='trailing':data+=b'X'
                    elif mutation=='generation':
                        struct.pack_into('<Q',data,12,0)
                        struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
                    elif mutation=='source_changed':
                        with closing(sqlite3.connect(mapped_path)) as db,db:
                            db.execute("UPDATE metadata SET value='changed' WHERE key='creator_json'")
                    elif mutation=='output_input_alias':command[8]=str(mapped_path)
                    elif mutation=='summary_input_alias':command[9]=str(handoff)
                    elif mutation=='summary_output_alias':command[9]=command[8]
                    elif mutation=='report_input_alias':command[2]=str(mapped_path)
                    request.write_bytes(data)
                    return original(runner,command,**kwargs)
                with mock.patch.object(rwroute_native,'NativeToolProcess',side_effect=launch):
                    with self.assertRaises(ValidationError):native(root,values)
                self.assertEqual(output.read_text(),'prior publication\n')
                if mutation!='source_changed':
                    self.assertEqual({p:p.read_bytes() for p in before},before)
                self.assertFalse(list(root.glob('.emuflow-rwroute-*')))
                self.assertFalse(list(root.glob('*.tmp.*')))

    def test_preloaded_index_keeps_logical_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);values=fixture(root)
            packed_view,placement_view=open_xilinx_physical_handoff(values[-1])
            logical_source=root/'canonical.json'
            logical_source.write_text(json.dumps(values[0]))
            with open_xilinx_mapped_index(values[-2]) as indexed,mock.patch.object(
                    rwroute_native,'resolve_native_executable',return_value=str(self.executable)):
                result=export_rwroute_input(logical_source,values[-1],values[-1],root/'native.tsv',
                    mapped_value=indexed,packed_value=packed_view,placement_value=placement_view,
                    source_sha256=DIGESTS)
            self.assertEqual(result['implementation_id'],'native-rwroute-export-v2')
            self.assertIn('META\tmapped_sha256\t'+DIGESTS['mapped_sha256'],(root/'native.tsv').read_text())

    def test_one_shot_differential_retires_payload_after_workers_exit(self):
        import importlib.util
        from types import SimpleNamespace
        script=Path(__file__).parents[1]/'scripts/validation/qualify_rwroute_native_export.py'
        spec=importlib.util.spec_from_file_location('rwroute_qualification',script)
        qualification=importlib.util.module_from_spec(spec);spec.loader.exec_module(qualification)
        for minimum in (1,1000000):
            with self.subTest(minimum=minimum),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);values=fixture(root)
                install=root/'runtime';(install/'bin').mkdir(parents=True)
                (install/'bin/emuflow-native').symlink_to(self.executable)
                io=root/'io.json';io.write_text(json.dumps(values[3]))
                args=SimpleNamespace(out=root/'comparison',mapped=values[-2],physical=values[-1],
                    expected_top='top',virtual_io_contract=io,resource_limits=None,
                    minimum_logical_cells=minimum,source_commit=None)
                with mock.patch.dict(os.environ,{'EMUFLOW_NATIVE_ROOT':str(install),
                        'PYTHONPATH':str(Path(__file__).parents[1]/'src')}):
                    if minimum==1:
                        result=qualification.qualify(args)
                        self.assertEqual(result['status'],'pass')
                        self.assertEqual(result['arms']['reference']['tsv_sha256'],result['arms']['native']['tsv_sha256'])
                    else:
                        with self.assertRaises(ValidationError):qualification.qualify(args)
                        result=json.loads((args.out/'qualification.json').read_text())
                        self.assertEqual(result['status'],'failed')
                self.assertFalse((args.out/'tmp').exists())
                self.assertFalse(list(args.out.rglob('input.tsv')))
                self.assertFalse(list(args.out.rglob('tmp')))
                self.assertTrue(values[-2].exists() and values[-1].exists())

    def test_native_clock_gate_closure_matches_independent_reference(self):
        for invalid in (False,True):
            with self.subTest(invalid=invalid),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);values=fixture(root)
                mapped,packed,placement,io,mp,handoff=values
                cells=mapped['modules']['top']['cells']
                cells['clock2']=copy.deepcopy(cells['clock'])
                cells['clock2']['connections']['I0']=[91]
                cells['clock2']['connections']['O']=[92]
                cells['z_ff']['connections']['C']=[92]
                assignment={'instance':'clock2','cell_type':'LUT2','bel':'A6LUT'}
                packed['clusters'].append({'id':'clock2','kind':'slice','site_templates':['SLICEL'],
                    'control_set':None,'assignments':[assignment]})
                placement['clusters'].append({'cluster':'clock2','site':'SLICE_X99Y0','site_type':'SLICEL',
                    'x':99,'y':0,'fixed':False,'physical_region':None,
                    'assignments':[dict(assignment,placement_mode='fixed',site='SLICE_X99Y0')]})
                if invalid:cells['dsp']['connections']['A'][0]=91
                mp.unlink();handoff.unlink();write_mapped(mp,mapped)
                write_xilinx_physical_handoff(handoff,packed,placement)
                def reference():
                    return export_rwroute_input(root/'m.json',root/'p.json',root/'l.json',root/'reference.tsv',
                        mapped_value=mapped,packed_value=packed,placement_value=placement,
                        source_sha256=DIGESTS,virtual_io_contract=io,
                        resource_limits={'lut':100,'ff':200,'dsp48':3})
                if invalid:
                    with self.assertRaises(ValidationError):reference()
                    with self.assertRaises(ValidationError):native(root,values)
                    self.assertFalse((root/'native.tsv').exists())
                else:
                    expected=reference();actual=native(root,values)
                    self.assertEqual(actual['qualified_clock_gates'],2)
                    self.assertEqual(actual['qualified_clock_gates'],expected['qualified_clock_gates'])
                    self.assertEqual((root/'native.tsv').read_bytes(),(root/'reference.tsv').read_bytes())

    def test_randomized_sparse_clock_graph_matches_reference_bytes(self):
        # Contract fixtures only. Real qualification continues to use NVDLA.
        # No FF clock sinks: invalid clock candidates may still be exported as
        # ordinary signal logic, exposing the complete closure result in TSV.
        for seed in range(24):
            with self.subTest(seed=seed),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);values=fixture(root)
                mapped,packed,placement,io,mp,handoff=values
                rng=random.Random(seed);count=12;cells={}
                def make(kind,connections,outputs,parameters=None):
                    return {'type':kind,'parameters':parameters or {},'attributes':{},
                        'connections':connections,'port_directions':{
                            p:'output' if p in outputs else 'input' for p in connections}}
                cells['enable']=make('FDRE',{'Q':[50]},['Q'])
                cells['ordinary']=make('LUT1',{'O':[51]},['O'])
                for i in range(count):
                    # Include explicit closed cycles and trees feeding cycles,
                    # then corrupt roots/enables/sinks to test propagation.
                    source=(i-1)%count if seed%4==0 else rng.randrange(count+3)
                    root_bit=1000+source if source<count else 60+source
                    enable=50 if rng.random()<0.85 else 51
                    cells[f'g{i:02d}']=make('LUT2',{'I0':[root_bit],'I1':[enable],'O':[1000+i]},['O'],
                        {'INIT':rng.choice(['1000','8',8])})
                for i in range(count):
                    if rng.random()<0.12:
                        cells[f'bad{i:02d}']=make('LUT1',{'I0':[1000+i],'O':[2000+i]},['O'])
                # Inout drivers exercise direction-code normalization and
                # stable driver ordering independently of source insertion.
                if seed%3==0:
                    cells['extra']=make('LUT1',{'O':[1000+seed%count]},['O'])
                    cells['extra']['port_directions']['O']='inout'
                items=list(cells.items());rng.shuffle(items);cells=dict(items)
                mapped['modules']['top'].update(cells=cells,ports={})
                packed['clusters']=[];packed['unplaced_constants']=[];placement['clusters']=[]
                for i,(name,value) in enumerate(cells.items()):
                    site=f'SLICE_X0Y{i}';bel='A6LUT' if value['type'].startswith('LUT') else 'AFF'
                    assignment={'instance':name,'cell_type':value['type'],'bel':bel}
                    packed['clusters'].append({'id':name,'kind':'slice','site_templates':['SLICEL'],
                        'control_set':None,'assignments':[assignment]})
                    placement['clusters'].append({'cluster':name,'site':site,'site_type':'SLICEL',
                        'x':0,'y':i,'fixed':False,'physical_region':None,
                        'assignments':[dict(assignment,placement_mode='fixed',site=site)]})
                mp.unlink();handoff.unlink();write_mapped(mp,mapped)
                write_xilinx_physical_handoff(handoff,packed,placement)
                expected=export_rwroute_input(root/'m.json',root/'p.json',root/'l.json',root/'reference.tsv',
                    mapped_value=mapped,packed_value=packed,placement_value=placement,source_sha256=DIGESTS,
                    resource_limits={'lut':100,'ff':200,'dsp48':3})
                actual=native(root,values,contract=False)
                self.assertEqual(actual['qualified_clock_gates'],expected['qualified_clock_gates'])
                self.assertEqual((root/'native.tsv').read_bytes(),(root/'reference.tsv').read_bytes())

    def test_sparse_v2_source_sequences_remain_native_and_byte_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);values=fixture(root)
            mapped,packed,placement,io,mp,handoff=values
            with closing(sqlite3.connect(mp)) as db,db:
                db.execute('UPDATE cells SET seq=1000000000000+seq*7')
                db.execute('UPDATE cell_pins SET cell_seq=1000000000000+cell_seq*7')
            expected=export_rwroute_input(root/'m.json',root/'p.json',root/'l.json',root/'reference.tsv',
                mapped_value=mapped,packed_value=packed,placement_value=placement,source_sha256=DIGESTS,
                virtual_io_contract=io,resource_limits={'lut':100,'ff':200,'dsp48':3})
            actual=native(root,values)
            self.assertEqual(actual['implementation_id'],'native-rwroute-export-v2')
            self.assertEqual(actual['routable_nets'],expected['routable_nets'])
            self.assertEqual(actual['runtime_breakdown']['source_cell_catalog_bytes'],24*len(mapped['modules']['top']['cells']))
            self.assertEqual((root/'native.tsv').read_bytes(),(root/'reference.tsv').read_bytes())
