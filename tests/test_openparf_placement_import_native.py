"""Native raw placement to independently audited physical handoff contracts."""
from contextlib import contextmanager, closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest import mock

import pytest

from emuflow.common.errors import ValidationError
from emuflow.physical.openparf import xilinx_openparf_atomic as atomic
from tests import openparf_placement_reference as reference
from emuflow.physical.openparf.placement_native import import_placement
from emuflow.physical.xilinx_physical_handoff import open_xilinx_physical_handoff
from tests.openparf_export_fixture import mapped_facts
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture


@contextmanager
def case(root,clocks=False):
    mapped,_,architecture=write_openparf_runtime_fixture(root)
    if clocks:
        doc=json.loads(mapped.read_text())
        for index,cell in enumerate(c for c in doc['modules']['top']['cells'].values() if c['type']=='FDRE'):
            cell['connections']['C']=[10+index%3]
            cell['connections']['CE']=[100+index%2]
            cell['parameters']={'IS_C_INVERTED':str(index%2)}
        mapped.write_text(json.dumps(doc))
    with mapped_facts(mapped) as owner:
        source_path=owner.path.with_name('atomic-source.json')
        source=atomic.build_xilinx_openparf_atomic_source(owner.path,source_path,mapped_value=owner)
        directory=root/'input'
        atomic.export_xilinx_openparf_atomic(owner.path,source_path,architecture,directory,
            mapped_value=owner,packed_value=source)
        selector=json.loads((directory/'name_map.json').read_text())
        with closing(sqlite3.connect(directory/selector['artifact'])) as db:
            atoms=db.execute('SELECT openparf,resource FROM atoms ORDER BY seq').fetchall()
        with closing(sqlite3.connect(directory/'site-map.sqlite3')) as db:
            lut=db.execute("SELECT dense_x,dense_y FROM resources WHERE resource='LUT' ORDER BY dense_x,dense_y").fetchall()
            ff=db.execute("SELECT dense_x,dense_y FROM resources WHERE resource='FF' ORDER BY dense_x,dense_y").fetchall()
        lines=[];counts={'LUT':0,'FF':0}
        for name,resource in atoms:
            n=counts[resource];counts[resource]+=1
            sites=lut if resource=='LUT' else ff
            per=8 if resource=='LUT' else 16
            x,y=sites[n//per];z=(n%8)*2+1 if resource=='LUT' else n%16
            lines.append(f'{name} {x} {y} {z}')
        placement=root/'native.pl';placement.write_text('# native discrete coordinates\n'+'\n'.join(lines)+'\n')
        yield dict(placement_path=placement,name_map_path=directory/'name_map.json',mapped_path=owner.path,
                   architecture_path=architecture,packed_path=source_path,report_path=root/'placement-import.json'),owner


def test_complete_import_generates_native_certificate_and_handoff(tmp_path):
    with case(tmp_path) as (options,owner):
        result=import_placement(**options)
        assert result['implementation_id']=='native-openparf-placement-import-v1'
        assert result['certificate']['summary']=={'atoms':128,'occupied_sites':8,'luts':64,'ffs':64,'hard_resources':{}}
        assert result['ff_control_legalization']['moved_ff_atoms']==0
        assert Path(result['placement'])==options['placement_path']
        packed,placed=open_xilinx_physical_handoff(Path(result['bridge']['physical_handoff']))
        assert packed['summary']['placed_cells']==128
        assert sum(len(c['assignments']) for c in placed['clusters'])==128
        # The retired producer remains only as an independent test oracle.
        expected=reference.validate_xilinx_openparf_atomic_placement(options['placement_path'],options['name_map_path'],
            options['mapped_path'],options['architecture_path'],mapped_value=owner,packed_path=options['packed_path'])
        actual=reference.load_xilinx_openparf_atomic_placement_clusters(Path(result['certificate_path']),result['certificate'])
        assert actual==expected['clusters']
        assert result['certificate']['source']==expected['source']


def test_ff_repair_matches_reference_coordinates_and_certificate(tmp_path):
    with case(tmp_path,clocks=True) as (options,owner):
        original=options['placement_path'].read_bytes()
        reference_path=tmp_path/'reference.pl';reference_path.write_bytes(original)
        oracle=reference.legalize_xilinx_openparf_ff_control_sets(reference_path,options['name_map_path'],options['mapped_path'],
            tmp_path/'reference-repair.json',mapped_value=owner,packed_path=options['packed_path'])
        result=import_placement(**options)
        assert result['ff_control_legalization']==oracle
        assert Path(result['placement']).read_bytes()==reference_path.read_bytes()
        assert options['placement_path'].read_bytes()==original
        expected=reference.validate_xilinx_openparf_atomic_placement(reference_path,options['name_map_path'],options['mapped_path'],
            options['architecture_path'],mapped_value=owner,packed_path=options['packed_path'])
        assert reference.load_xilinx_openparf_atomic_placement_clusters(Path(result['certificate_path']),result['certificate'])==expected['clusters']


@pytest.mark.parametrize('damage',['missing','duplicate','unknown','fractional','nan','lut-even','outside','ff-slot'])
def test_bad_placement_preserves_accepted_report_and_payloads(tmp_path,damage):
    with case(tmp_path) as (options,owner):
        accepted=import_placement(**options)
        original=options['report_path'].read_bytes()
        rows=options['placement_path'].read_text().splitlines()
        if damage=='missing':rows.pop()
        elif damage=='duplicate':rows.append(rows[1])
        elif damage=='unknown':rows[1]='unknown 0 0 1'
        else:
            at=next(i for i,r in enumerate(rows) if r and not r.startswith('#') and int(r.split()[-1])%2==1)
            fields=rows[at].split()
            if damage=='fractional':fields[1]='0.5'
            if damage=='nan':fields[1]='NaN'
            if damage=='lut-even':
                selector=json.loads(options['name_map_path'].read_text())
                with closing(sqlite3.connect(options['name_map_path'].with_name(selector['artifact']))) as db:
                    name=db.execute("SELECT openparf FROM atoms WHERE resource='LUT' ORDER BY seq LIMIT 1").fetchone()[0]
                at=next(i for i,r in enumerate(rows) if r.split() and r.split()[0]==name)
                fields=rows[at].split();fields[3]='0'
            if damage=='outside':fields[1]='9999'
            if damage=='ff-slot':
                selector=json.loads(options['name_map_path'].read_text())
                with closing(sqlite3.connect(options['name_map_path'].with_name(selector['artifact']))) as db:
                    name=db.execute("SELECT openparf FROM atoms WHERE resource='FF' ORDER BY seq LIMIT 1").fetchone()[0]
                at=next(i for i,r in enumerate(rows) if r.split() and r.split()[0]==name)
                fields=rows[at].split();fields[3]='16'
            rows[at]=' '.join(fields)
        options['placement_path'].write_text('\n'.join(rows)+'\n')
        with pytest.raises(ValidationError):import_placement(**options)
        assert options['report_path'].read_bytes()==original
        assert Path(accepted['certificate_path']).is_file()
        assert Path(accepted['bridge']['physical_handoff']).is_file()


@contextmanager
def carry_case(root):
    from tests.xilinx_carry_fixture import _write_fixture
    from tests.test_xilinx_openparf_atomic_carry_integration import _write_atomic_placement
    mapped,_,architecture,native,provider=_write_fixture(root)
    with mapped_facts(mapped) as owner:
        source_path=owner.path.with_name('atomic-source.json')
        source=atomic.build_xilinx_openparf_atomic_source(owner.path,source_path,mapped_value=owner)
        directory=root/'input'
        atomic.export_xilinx_openparf_atomic(owner.path,source_path,architecture,directory,
            mapped_value=owner,packed_value=source,native_constraints_path=native,provider_manifest_path=provider)
        placement=root/'native.pl'
        with reference.read_xilinx_openparf_atomic_name_map(directory/'name_map.json') as view:
            _write_atomic_placement(placement,view)
        yield dict(placement_path=placement,name_map_path=directory/'name_map.json',mapped_path=owner.path,
            architecture_path=architecture,packed_path=source_path,report_path=root/'placement-import.json',
            native_constraints_path=native,provider_manifest_path=provider),owner


def test_native_carry_macro_windows_topology_and_adjacency_are_all_checked(tmp_path):
    with carry_case(tmp_path) as (options,owner):
        result=import_placement(**options)
        assert result['certificate']['summary']['carry8_macros']==2
        assert result['certificate']['summary']['native_carry_edges']==1
        expected=reference.validate_xilinx_openparf_atomic_placement(options['placement_path'],options['name_map_path'],options['mapped_path'],
            options['architecture_path'],mapped_value=owner,packed_path=options['packed_path'],
            native_constraints_path=options['native_constraints_path'],provider_manifest_path=options['provider_manifest_path'])
        assert result['certificate']['summary']==expected['summary']
        assert reference.load_xilinx_openparf_atomic_placement_clusters(Path(result['certificate_path']),result['certificate'])==expected['clusters']


@pytest.mark.parametrize('damage',['chain-gap','lut-slot','window-count','unused-set'])
def test_carry_window_and_occupancy_contract_fail_closed(tmp_path,damage):
    with carry_case(tmp_path) as (options,owner):
        if damage in ('chain-gap','lut-slot'):
            selector=json.loads(options['name_map_path'].read_text())
            with closing(sqlite3.connect(options['name_map_path'].with_name(selector['artifact']))) as db:
                instances=dict(db.execute('SELECT openparf,instance FROM atoms'))
            rows=[]
            for line in options['placement_path'].read_text().splitlines():
                fields=line.split();name=instances[fields[0]]
                if damage=='chain-gap' and name.startswith('carry1'):fields[2]='2'
                if damage=='lut-slot' and name=='carry0$lut0':fields[3]='3'
                rows.append(' '.join(fields))
            options['placement_path'].write_text('\n'.join(rows)+'\n')
        else:
            path=options['name_map_path'].with_name('physical-macro-groups.json')
            doc=json.loads(path.read_text())
            if damage=='window-count':doc['groups'][0]['window_count']+=1
            else:doc['window_sets'].append({'id':'unused','resource':'DSP48E2','windows':[[{'site':'S0','z':0,'claims':['site:S0']}]]})
            path.write_text(json.dumps(doc))
        with pytest.raises(ValidationError):import_placement(**options)
        assert not options['report_path'].exists()


@contextmanager
def macro_case(root,primitive):
    from tests import test_xilinx_openparf_atomic as fixture
    from tests.openparf_runtime_fixture import write_openparf_ramb18_fixture,write_openparf_hardblock_cascade_fixture
    from tests.openparf_device_fixture import seal_native_input
    native,provider=root/'native.json',root/'provider.json'
    if primitive.startswith('MUX'):
        mapped,_,architecture=fixture._fixture(root)
        doc=json.loads(mapped.read_text());doc['modules']['top']['cells']=fixture._mux_tree_cells(primitive)
        mapped.write_text(json.dumps(doc))
        fixture._add_mux_bels(architecture,{'MUXF7','MUXF8','MUXF9'})
        native=provider=None
    else:
        if primitive=='RAMB18E2':mapped,_,architecture=write_openparf_ramb18_fixture(root)
        else:mapped,_,architecture=write_openparf_runtime_fixture(root,include_hard=True)
        arch=json.loads(architecture.read_text())
        if primitive=='mixed-hard':
            for bel,kind,mode,z in (('RAMB18E2_L','RAMB18E2','RAMB180',0),('RAMB18E2_U','RAMB18E2','RAMB181',1),('RAMB36E2','RAMB36E2','RAMB36',0)):
                row={'name':bel,'type':kind,'z':z,'compatible_cells':[kind],'placement_mode':mode}
                arch['site_templates'][mode]={'bels':[row],'alternative_templates':[]}
            arch['site_templates']['RAMB181']['alternative_templates']=['RAMB180','RAMB36']
            index=0
            for site in arch['sites']:
                if site['type']=='RAMB36E2':
                    site.update(name=f'RAMB18_X{index}Y1',type='RAMB181',template='RAMB181');index+=1
            architecture.write_text(json.dumps(arch))
        supplied={'payload':{'bram_tile_groups':[],'dedicated_adjacency':[]}}
        if primitive=='RAMB18E2':anchors=[f'RAMB18_X{i}Y1' for i in range(2)]
        elif primitive=='mixed-hard':anchors=[s['name'] for s in arch['sites'] if s['type']=='RAMB181']
        else:anchors=[]
        for i,anchor in enumerate(anchors):
            supplied['payload']['bram_tile_groups'].append({'anchor':anchor,'tile':f'BRAM_TEST_X{i}',
                'lower':{'site':f'RAMB18_X{i}Y0','site_type':'RAMB180','bel':'RAMB18E2_L'},
                'upper':{'site':f'RAMB18_X{i}Y1','site_type':'RAMB181','bel':'RAMB18E2_U'},
                'whole':{'site':f'RAMB36_X{i}Y0','site_type':'RAMB36','bel':'RAMB36E2'}})
        seal_native_input(architecture,native,provider,supplied)
    with mapped_facts(mapped) as owner:
        source_path=owner.path.with_name('atomic-source.json')
        source=atomic.build_xilinx_openparf_atomic_source(owner.path,source_path,mapped_value=owner)
        directory=root/'input'
        atomic.export_xilinx_openparf_atomic(owner.path,source_path,architecture,directory,
            mapped_value=owner,packed_value=source,native_constraints_path=native,provider_manifest_path=provider)
        selector=json.loads((directory/'name_map.json').read_text())
        with closing(sqlite3.connect(directory/selector['artifact'])) as db:
            atoms=db.execute('SELECT openparf,instance,resource FROM atoms ORDER BY seq').fetchall()
        with closing(sqlite3.connect(directory/'site-map.sqlite3')) as db:
            slices=db.execute("SELECT dense_x,dense_y FROM resources WHERE resource='LUT' ORDER BY dense_x,dense_y").fetchall()
            physical={(r,n):(x,y) for r,n,x,y in db.execute('SELECT resource,physical_site,dense_x,dense_y FROM physical_sites')}
        contract=json.loads((directory/'physical-macro-groups.json').read_text())
        window_sets={r['id']:r for r in contract.get('window_sets',[])};positions={}
        for g in contract['groups']:
            if g['kind']=='site_macro':
                members=g['window_template']['members']
                for name,m in zip(g['source_instances'],members):positions[name]=(*slices[0],m['z'])
            else:
                windows=window_sets[g['window_set']]['windows'] if g.get('window_set') else g['windows']
                window=windows[0]
                if primitive=='RAMB18E2' and positions:
                    first=next(iter(positions.values()))
                    window=next(w for w in windows if physical[(g['resource'],w[0]['anchor'])]==first[:2] and w[0]['z']!=first[2])
                for name,entry in zip(g['source_instances'],window):
                    anchor=entry.get('anchor',entry['site']);positions[name]=(*physical[(g['resource'],anchor)],entry['z'])
        counts={'LUT':0,'FF':0};lines=[]
        for name,instance,resource in atoms:
            if instance in positions:x,y,z=positions[instance]
            else:
                n=counts[resource];counts[resource]+=1
                x,y=slices[n//8];z=2*(n%8)+(1 if resource=='LUT' else 0)
            lines.append(f'{name} {x} {y} {z}')
        placement=root/'native.pl';placement.write_text('\n'.join(lines)+'\n')
        yield dict(placement_path=placement,name_map_path=directory/'name_map.json',mapped_path=owner.path,
            architecture_path=architecture,packed_path=source_path,report_path=root/'placement-import.json',
            native_constraints_path=native,provider_manifest_path=provider),owner


@pytest.mark.parametrize('primitive',['MUXF7','MUXF8','MUXF9','RAMB18E2','mixed-hard'])
def test_macro_bel_and_hardblock_tile_views_match_reference(tmp_path,primitive):
    with macro_case(tmp_path,primitive) as (options,owner):
        result=import_placement(**options)
        expected=reference.validate_xilinx_openparf_atomic_placement(options['placement_path'],options['name_map_path'],options['mapped_path'],
            options['architecture_path'],mapped_value=owner,packed_path=options['packed_path'],
            native_constraints_path=options['native_constraints_path'],provider_manifest_path=options['provider_manifest_path'])
        assert result['certificate']['summary']==expected['summary']
        assert reference.load_xilinx_openparf_atomic_placement_clusters(Path(result['certificate_path']),result['certificate'])==expected['clusters']
        if primitive=='mixed-hard':
            assert result['certificate']['summary']['hard_resources']=={'DSP48E2':1,'RAMB36E2':1,'URAM288':1}


def test_import_production_module_has_no_replaced_graph_implementations(tmp_path):
    from emuflow.platform.architecture import ArchitectureDB
    from emuflow.physical.xilinx_netlist import _IndexedCells
    for name in ('_IndexedAtomicPlacement','_AtomicPlacementCertificateBuilder',
        'legalize_xilinx_openparf_ff_control_sets','validate_xilinx_openparf_atomic_placement',
        'write_xilinx_openparf_atomic_placement_certificate','XilinxOpenparfAtomicPlacementView'):
        assert not hasattr(atomic,name)
    with (
        case(tmp_path) as (options,owner),
        mock.patch.object(reference,'legalize_xilinx_openparf_ff_control_sets',side_effect=AssertionError('reference legalization')),
        mock.patch.object(reference,'validate_xilinx_openparf_atomic_placement',side_effect=AssertionError('reference placement')),
        mock.patch.object(ArchitectureDB,'load',side_effect=AssertionError('host device population')),
        mock.patch.object(_IndexedCells,'__iter__',side_effect=AssertionError('host mapped population')),
    ):
        assert import_placement(**options)['bridge']['validation']['packing']['status']=='pass'


def test_handoff_reader_requires_the_validated_native_generation(tmp_path):
    from emuflow.common.native_tools import native_file_generation
    with case(tmp_path) as (options,owner):
        result=import_placement(**options)
        path=Path(result['bridge']['physical_handoff'])
        assert result['handoff_generation']==list(native_file_generation(path))
        open_xilinx_physical_handoff(path,validated_sha256=result['bridge']['physical_handoff_sha256'],
                                    validated_generation=result['handoff_generation'])
        path.chmod(path.stat().st_mode ^ 0o100)
        with pytest.raises(ValidationError,match='validated generation'):
            open_xilinx_physical_handoff(path,validated_sha256=result['bridge']['physical_handoff_sha256'],
                                        validated_generation=result['handoff_generation'])


def test_host_rejects_output_mutation_after_native_completion(tmp_path):
    from emuflow.physical.openparf import placement_native
    real=placement_native.NativeToolProcess
    class Mutated(real):
        def finish(self):
            value=super().finish()
            if value.returncode==0:
                report=json.loads((tmp_path/'placement-import.json').read_text())
                path=Path(report['bridge']['physical_handoff'])
                path.chmod(path.stat().st_mode ^ 0o100)
            return value
    with case(tmp_path) as (options,owner),mock.patch.object(placement_native,'NativeToolProcess',Mutated):
        with pytest.raises(ValidationError,match='output generation changed'):
            import_placement(**options)


def test_native_metrics_source_seal_is_not_replaceable_by_metadata(tmp_path):
    with case(tmp_path) as (options,owner):
        path=tmp_path/'metrics.json';path.write_text('{"status":"pass"}')
        metadata={'native_convergence':{'artifact':{'path':str(path),'bytes':path.stat().st_size,'sha256':'0'*64},'metrics':{}}}
        with pytest.raises(ValidationError,match='convergence artifact seal'):
            import_placement(**options,metadata=metadata)
        assert not options['report_path'].exists()


def test_validation_without_repair_refuses_conflicting_ff_controls(tmp_path):
    with case(tmp_path,clocks=True) as (options,owner):
        before=options['placement_path'].read_bytes()
        with pytest.raises(ValidationError,match='control legality'):
            import_placement(**options,legalize_ff=False)
        assert options['placement_path'].read_bytes()==before
        assert not options['report_path'].exists()
