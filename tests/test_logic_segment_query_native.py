"""Complete native logic query ownership; NVDLA remains terminal acceptance."""
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.timing.logic_segment_identity_storage import read_logic_segment_identity_storage
from emuflow.common.native_tools import native_file_generation
from emuflow.common.native_protocol import CheckedRequestWriter
from tests.native_build import emuflow_native
from tests.logic_segment_query_fixture import sources, prepare, query


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))


@pytest.mark.parametrize('backend',['xilinx','vivado'])
@pytest.mark.parametrize('members',[1,7])
def test_whole_native_query_preserves_every_member_and_exact_semantic(tmp_path,backend,members):
    value=sources(tmp_path/'input',members=members)
    with (patch('emuflow.ir.ir.EmuIR.load',side_effect=AssertionError('Python full source graph')),
          patch('emuflow_tools.timing.sta.iter_sta_path_database_paths',side_effect=AssertionError('Python path records'))):
        import emuflow.partition.phase3_assignment_index as retired_exact
        assert not hasattr(retired_exact, 'read_phase3_exact_semantic_partition')
        import emuflow.scheduling.tdm as tdm
        assert not hasattr(tdm, 'reconstruct_tdm_schedule_timing_paths')
        prepared=prepare(value)
        try:
            reports=[query(value,tmp_path/f'{backend}-{fpga}',backend=backend,fpga=fpga,prepared=prepared)
                     for fpga in ('fpga0','fpga1')]
        finally:
            prepared.close()
    assert prepared.original_ir is None
    assert not any(hasattr(prepared,name) for name in ('path_endpoints','database_paths','route_timing',
        'route_by_net','timing_records','original_nets','original_instances','exact_launches'))
    all_rows=[]
    for report in reports:
        assert report['implementation_id']=='native-logic-segment-query-v1'
        with read_logic_segment_identity_storage(Path(report['identity'])) as identity:
            assert identity.validate_identity()['segments']==report['segments']
            rows=list(identity['segments']);all_rows.extend(rows)
            assert identity['semantic_contract_sha256']==value.assignment_value['semantic_contract_sha256']
        lines=Path(report['query']).read_text().splitlines()
        assert len(lines)==report['segments']+1
        assert not (Path(report['identity']).parent.parent/'identity.json').exists()
        if backend=='vivado':
            assert all(row['start_object_kind'] in {'pin','port'} for row in rows)
            assert all('cone_anchor_pin' in row for row in rows if row['kind']!='capture')
            assert all(len(line.split('\t'))==8 for line in lines)
        else:
            assert all(len(line.split('\t'))==4 for line in lines)
    assert len(all_rows)==3*members
    assert {row['static_exact_segment_id'] for row in all_rows}=={'segment000000','segment000001','segment000002'}
    for member in value.members:
        chosen=[row for row in all_rows if row['member_path']==member]
        assert {(row['kind'],row['fpga']) for row in chosen}=={('launch','fpga0'),('transition','fpga1'),('capture','fpga0')}
        assert all(row['system_path']==value.members[0] for row in chosen)
        assert next(row for row in chosen if row['kind']=='launch')['start_pin']==('cell:q0/Q' if backend=='xilinx' else 'q0/Q')
        assert next(row for row in chosen if row['kind']=='capture')['end_pin']==('cell:q1/D' if backend=='xilinx' else 'q1/D')


@pytest.mark.parametrize('backend',['xilinx','vivado'])
def test_all_assignment_semantics_are_generated_without_a_timing_representative(tmp_path,backend):
    value=sources(tmp_path/'input',paths=False)
    rows=[]
    for fpga in ('fpga0','fpga1'):
        report=query(value,tmp_path/fpga,backend=backend,fpga=fpga)
        with read_logic_segment_identity_storage(Path(report['identity'])) as identity:
            rows.extend(identity['segments'])
        assert report['exact_member_paths']==0
    assert {row['static_exact_segment_id'] for row in rows}=={'segment000000','segment000001','segment000002'}
    assert {row['kind'] for row in rows}=={'launch','transition','capture'}
    assert all(row['member_path'].startswith('static-exact-contract:') for row in rows)


@pytest.mark.parametrize('backend',['xilinx','vivado'])
def test_full_fanin_includes_launch_absent_from_representative(tmp_path,backend):
    value=sources(tmp_path/'input',extra_launch=True)
    report=query(value,tmp_path/'output',backend=backend)
    with read_logic_segment_identity_storage(Path(report['identity'])) as identity:
        launches=[row for row in identity['segments'] if row['kind']=='launch']
    assert {row['start_pin'] for row in launches}==({'cell:q0/Q','cell:q_extra/Q'} if backend=='xilinx' else {'q0/Q','q_extra/Q'})
    assert {row['static_exact_segment_id'] for row in launches}=={'segment000000'}
    assert sum(row['member_path'].startswith('static-exact-contract:') for row in launches)==1


def test_unsupported_member_is_reported_but_complete_assignment_coverage_remains(tmp_path):
    value=sources(tmp_path/'input',wrong_owner=True)
    report=query(value,tmp_path/'output')
    assert report['unsupported_member_paths']==1 and report['exact_member_paths']==0
    with read_logic_segment_identity_storage(Path(report['identity'])) as identity:
        assert identity['unsupported_member_paths'][0]['path']==value.members[0]
        assert 'partition mismatch' in identity['unsupported_member_paths'][0]['reason']
        assert {row['kind'] for row in identity['segments']}=={'launch','capture'}



@pytest.mark.parametrize('backend',['xilinx','vivado'])
def test_configuration_stable_constant_preserves_typed_contract_without_fake_query(tmp_path,backend):
    from contextlib import closing
    from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index, _exact_binary_reference
    from emuflow.partition.phase3_exact_binary import iter_phase3_exact_segments
    from emuflow_tools.physical.physical_coverage import build_physical_coverage
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule

    value=sources(tmp_path/'input',paths=False,constant_launch=True)
    # Inspect the published authoritative binary, rather than the fixture's
    # in-memory declaration, before consuming it through the native stage.
    with closing(open_phase3_assignment_index(value.assignment)) as db:
        exact,descriptor=_exact_binary_reference(db,value.assignment)
    authoritative=list(iter_phase3_exact_segments(exact,descriptor,'fpga0'))
    constant=next(row for row in authoritative if row['id']=='segment000000')
    assert constant['source_semantics']=='configuration-stable-constant'
    assert {row['id'] for row in authoritative}=={'segment000000','segment000002'}
    rows=[]
    for fpga in ('fpga0','fpga1'):
        report=query(value,tmp_path/fpga,backend=backend,fpga=fpga)
        assert report['unsupported_member_paths']==0 and report['exact_member_paths']==0
        with read_logic_segment_identity_storage(Path(report['identity'])) as identity:
            rows.extend(identity['segments'])
        assert all(line.split('\t')[1]!='launch' for line in Path(report['query']).read_text().splitlines()[1:])
    assert {row['static_exact_segment_id'] for row in rows}=={'segment000001','segment000002'}
    assert {row['kind'] for row in rows}=={'transition','capture'}
    assert all(row['static_exact_segment_id']!='segment000000' for row in rows)

    # Query construction must not invent a physical launch measurement.
    # Native whole-assignment coverage owns structural zero qualification;
    # genuinely unmeasured dynamic segments remain explicitly missing.
    contract=json.loads((value.schedule.parent/'cross_layer_timing.json').read_text())
    schedule=read_tdm_schedule(value.schedule)
    physical={'provider':'fixture','qualification':'fixture',
        'boundary_identities':{},'boundary_timing':{},'logic_segment_timing':{}}
    with build_physical_coverage(contract,schedule,physical,value.platform,value.assignment) as coverage:
        structural=next(row for row in coverage['logic_segment_bindings'] if row['segment']=='segment000000')
        assert structural['measurement']=='structural-configuration-stable-constant'
        assert structural['measured_min_delay_ns']==structural['measured_max_delay_ns']==0.0
        assert coverage['metrics']['required_logic_segments']==3
        assert set(coverage['missing']['logic_segments'])=={'segment000001','segment000002'}
        assert coverage['status']=='incomplete'

@pytest.mark.parametrize('damage',['original','assignment','schedule','boundary'])
def test_changed_source_rejects_new_generation_without_replacing_previous_outputs(tmp_path,damage):
    value=sources(tmp_path/'input')
    prepared=prepare(value)
    report=query(value,tmp_path/'output',prepared=prepared)
    before={p:p.read_bytes() for p in Path(report['identity']).parent.iterdir()}
    target={'original':value.original,'assignment':value.assignment,'schedule':prepared.sources[4].path,
            'boundary':value.boundaries['fpga0']}[damage]
    if damage=='boundary':
        payload=json.loads(target.read_text());payload['header']['fpga']='foreign';target.write_text(json.dumps(payload))
    else:
        target.chmod(target.stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError):query(value,tmp_path/'output',prepared=prepared)
    assert {p:p.read_bytes() for p in before}==before
    assert len(list((tmp_path/'output').glob('identity.json.generation.*')))==1
    prepared.close()


@pytest.mark.parametrize('damage',['crc','truncated','generation','schedule-row','boundary-peer','fanin-owner'])
def test_native_direct_request_rejects_corruption_before_publication(tmp_path,damage):
    import subprocess
    from emuflow_tools.timing.logic_segment_query_native import _write_inputs
    value=sources(tmp_path/'input');prepared=prepare(value)
    root=tmp_path/'request';root.mkdir()
    refs,_=_write_inputs(prepared,value.merged['fpga0'],value.boundaries['fpga0'],'fpga0','xilinx','query.tsv','identity.json',root)
    request=refs[-1].path
    if damage in {'crc','truncated'}:
        raw=bytearray(request.read_bytes())
        if damage=='crc':raw[20]^=0x40
        else:raw=raw[:-17]
        request.write_bytes(raw)
    else:
        if damage=='schedule-row':
            with sqlite3.connect(refs[4].path) as db:db.execute('UPDATE schedule_entries SET lane=lane+100 WHERE seq=0')
        elif damage=='boundary-peer':
            with sqlite3.connect(refs[7].path) as db:db.execute("UPDATE endpoints SET peer='foreign'")
        elif damage=='fanin-owner':
            # A local cut's combinational driver belongs to another partition.
            # Its representative path still has valid architectural endpoints;
            # complete cone validation must reject this undeclared boundary.
            with sqlite3.connect(refs[0].path) as db:
                cell=db.execute("SELECT seq FROM cell_records WHERE id='l0'").fetchone()[0]
            with sqlite3.connect(refs[1].path) as db:
                owner=db.execute("SELECT part_index FROM parts WHERE fpga='fpga1'").fetchone()[0]
                db.execute('UPDATE copies SET part_index=? WHERE source_instance_seq=? AND is_replica=0',(owner,cell))
        # Rebind current source generations while retaining the original
        # logical seals. Independent native audits must still reject bad rows.
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QI',0x31514C50464D5545,1)
            for index,ref in enumerate(refs[:-1]):
                generation=list(native_file_generation(ref.path))
                if damage=='generation' and index==0:generation[1]+=1
                writer.put('3Q4q',*generation)
            config=dict(prepared.request,fpga='fpga0',provider='xilinx',query_name='query.tsv',identity_name='identity.json',report_name='logic-query-report.json')
            writer.text(json.dumps(config,separators=(',',':'),sort_keys=True));writer.finish()
        finally:writer.close()
    output=tmp_path/'generation'
    completed=subprocess.run([str(emuflow_native()),'physical','query-logic-segments',*(str(ref.path) for ref in refs),str(output)],capture_output=True,text=True)
    assert completed.returncode!=0
    assert not output.exists() and not list(tmp_path.glob('.logic-query-*'))
    if damage=='fanin-owner':
        assert 'launch cone crosses undeclared partition boundary' in completed.stderr+completed.stdout
    else:
        assert any(word in completed.stderr+completed.stdout for word in ('seal','generation','CRC','request','physical evidence'))
    prepared.close()
