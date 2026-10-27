"""Published semantic owner -> native integer fanins, without a host graph."""
from contextlib import closing
import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess

import pytest

from emuflow.phase3_assignment_index import write_phase3_assignment_index, iter_phase3_cut_records
from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT, read_phase3_exact_binary_contract
from tests.native_build import exact_semantic_view_test
from tests.test_phase3_assignment_direct_cuts import _assignment
from tests.test_phase3_clusters_native import source


def prepare(source,tmp_path,mode='normal'):
    path=tmp_path/'assignment.sqlite'
    if mode=='transitions':
        from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
        case=StaticExactCombinationalCutPartitionTest();case.setUp()
        _clusters,assignment=case._exact_artifacts(dependent_return=True,dependency_depth=2)
        write_phase3_assignment_index(path,case.ir,assignment)
    else:
        with _assignment(source,mode)as(ir,assignment,_typed):
            write_phase3_assignment_index(path,ir,assignment)
    cut_records=list(iter_phase3_cut_records(path))
    with closing(sqlite3.connect(path))as db:
        descriptor=json.loads(db.execute("SELECT value FROM metadata WHERE key='semantic_contract_binary'").fetchone()[0])
        assignment_header={key:json.loads(value)for key,value in db.execute("SELECT key,value FROM metadata")}
        rows=list(db.execute('SELECT cut_index,net_id,source_part_index FROM cuts ORDER BY cut_index'))
    binary=path.with_name(EXACT_BINARY_ARTIFACT)
    contract=read_phase3_exact_binary_contract(binary,descriptor,{**assignment_header,'cut_nets':cut_records})
    return path,binary,descriptor,contract,rows


@pytest.mark.parametrize('mode',['normal','unicode','sparse','no-cuts','transitions'])
def test_native_fanins_match_independent_logical_reconstruction(source,tmp_path,mode):
    path,binary,descriptor,contract,cuts=prepare(source,tmp_path,mode)
    result=subprocess.run([str(exact_semantic_view_test()),str(path),str(binary)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    actual=json.loads(result.stdout)
    # The binary's part catalog is independently paired with the index.
    with closing(sqlite3.connect(path))as db:
        parts=[r[0]for r in db.execute('SELECT fpga FROM parts ORDER BY part_index')]
    net_ids={row[1]:row[0]for row in cuts}
    segments=[None]*len(contract['logic_segments'])
    for segment in contract['logic_segments']:
        number=int(segment['id'][7:]);kind=['launch_to_tx','rx_to_tx','rx_to_capture'].index(segment['kind'])
        segments[number]=[net_ids[segment['source_cut_net']]if kind else 2**32-1,
                          int(segment['capture_requirement'][7:])if kind==2 else net_ids[segment['sink_cut_net']],
                          parts.index(segment['fpga']),kind,segment.get('source_semantics')=='configuration-stable-constant']
    assert actual['parts']==parts
    assert actual['segments']==segments
    if mode=='transitions':assert {row[3]for row in segments}=={0,1,2}
    assert actual['sources']==[[int(identity[7:])for identity in node['source_segment_ids']]for node in contract['cut_nodes']]
    assert actual['source_parts']==[row[2]for row in cuts]
    capture_segments={segment['capture_requirement']:int(segment['id'][7:])
                      for segment in contract['logic_segments']if segment['kind']=='rx_to_capture'}
    captures=[]
    for capture in contract['capture_requirements']:
        state=capture['kind']=='architectural-state'
        bit=capture.get('bit',0)
        captures.append([int(capture['id'][7:]),capture_segments[capture['id']],net_ids[capture['cut_net']],
                         parts.index(capture['fpga']),int(state),capture['endpoint'],capture.get('port',''),
                         bit.to_bytes(max(1,(bit.bit_length()+7)//8),'big').hex()if state else ''])
    assert actual['captures']==sorted(captures,key=lambda row:(row[3],row[0]))
    assert actual['semantic_sha256']==descriptor['semantic_contract_sha256']


def test_identical_sidecar_from_another_owner_is_not_implicitly_substituted(source,tmp_path):
    path,binary,_descriptor,_contract,_cuts=prepare(source,tmp_path)
    other=tmp_path/'other';other.mkdir();copy=other/binary.name;shutil.copyfile(binary,copy)
    result=subprocess.run([str(exact_semantic_view_test()),str(path),str(copy)],capture_output=True,text=True)
    assert result.returncode!=0 and 'another assignment location'in result.stderr


def test_owner_pair_relocation_preserves_integer_fanins(source,tmp_path):
    original=tmp_path/'original';original.mkdir()
    path,binary,_descriptor,_contract,_cuts=prepare(source,original,'transitions')
    moved=tmp_path/'moved';shutil.copytree(original,moved)
    command=[str(exact_semantic_view_test())]
    before=subprocess.run([*command,str(path),str(binary)],capture_output=True,text=True,check=True)
    after=subprocess.run([*command,str(moved/path.name),str(moved/binary.name)],capture_output=True,text=True,check=True)
    assert after.stdout==before.stdout


def test_large_repeated_source_groups_use_only_typed_owner_pair(source,tmp_path):
    # Representation qualification only: neither a partition search nor QoR
    # evidence. The consumer receives only the published SQLite/binary pair.
    from emuflow.phase3_exact_binary import write_phase3_exact_binary
    from tests.test_phase3_exact_binary import _contract
    path,binary,_descriptor,_contract_value,_cuts=prepare(source,tmp_path)
    contract=_contract('native_source_groups')
    contract.update(cut_nodes=[],logic_segments=[],capture_requirements=[],dependency_edges=[])
    count=20_000
    for cut in range(count):
        net=f'net{cut:07d}';part=('a','b')[cut%2]
        identities=[f'segment{cut*4+i:06d}'for i in range(4)]
        contract['cut_nodes'].append(dict(net=net,cut_class='combinational',source_fpgas=[part],
            sink_fpgas=[('a','b')[1-cut%2]],dependency_level=0,combinational_dependency_depth=0,
            predecessor_cut_nets=[],source_segment_ids=identities,capture_segment_ids=[]))
        contract['logic_segments'].extend(dict(id=identity,kind='launch_to_tx',fpga=part,sink_cut_net=net)
            for identity in identities)
    contract['metrics']=dict(transported_cut_nets=count,logic_segments=count*4,capture_requirements=0,dependency_edges=0)
    descriptor=write_phase3_exact_binary(binary,contract,['a','b'])
    with closing(sqlite3.connect(path))as db, db:
        db.execute('DELETE FROM cut_predecessors');db.execute('DELETE FROM cut_sinks');db.execute('DELETE FROM cuts')
        db.executemany('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)',
            ((cut,f'net{cut:07d}',cut%2,'combinational',1,None,0,0)for cut in range(count)))
        db.executemany('INSERT INTO cut_sinks VALUES(?,?,?)',
            ((f'net{cut:07d}',cut%2,1-cut%2)for cut in range(count)))
        db.executemany('UPDATE metadata SET value=? WHERE key=?',[
            (json.dumps(value),key)for key,value in dict(cut_records=count,semantic_contract_binary=descriptor,
                semantic_contract_sha256=descriptor['semantic_contract_sha256']).items()])
    result=subprocess.run([str(exact_semantic_view_test()),str(path),str(binary),'summary'],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)==dict(cuts=count,segments=count*4,fanins=count*4)
    assert len(result.stdout)<100


def rewrite(path,binary,descriptor,manifest,payload):
    encoded=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    data=bytearray(payload)+encoded
    struct.pack_into('<8sIQQ',data,0,b'EMUEXACT',2,len(payload),len(encoded))
    binary.write_bytes(data)
    descriptor={**descriptor,'bytes':len(data),'manifest_sha256':hashlib.sha256(encoded).hexdigest()}
    with closing(sqlite3.connect(path))as db, db:
        db.execute("UPDATE metadata SET value=? WHERE key='semantic_contract_binary'",(json.dumps(descriptor),))


def test_all_capture_endpoints_stream_without_a_selected_path_library(source,tmp_path):
    from tests.test_phase3_exact_binary import _contract
    from emuflow.phase3_exact_binary import write_phase3_exact_binary
    path,binary,_descriptor,_old_contract,_cuts=prepare(source,tmp_path)
    contract=_contract('all_capture_endpoints');count=5000
    contract.update(cut_nodes=[],logic_segments=[],capture_requirements=[],dependency_edges=[])
    for cut in range(2):
        contract['cut_nodes'].append(dict(net=f'net{cut}',cut_class='combinational',source_fpgas=[('a','b')[cut]],
            sink_fpgas=[('a','b')[1-cut]],dependency_level=0,combinational_dependency_depth=0,
            predecessor_cut_nets=[],source_segment_ids=[f'segment{cut:06d}'],capture_segment_ids=[]))
        contract['logic_segments'].append(dict(id=f'segment{cut:06d}',kind='launch_to_tx',fpga=('a','b')[cut],sink_cut_net=f'net{cut}'))
    expected=[]
    for i in range(count):
        cut=i%2;part=1-cut;state=i%3!=0;segment=f'segment{i+2:06d}';capture=f'capture{i:06d}'
        endpoint=f'state\\雪/hier\x00{i}/D'if state else f'top:output[{i}]'
        row=dict(id=capture,cut_net=f'net{cut}',fpga=('a','b')[part],kind='architectural-state'if state else'top-output',endpoint=endpoint)
        bit=[0,256,1<<64,1<<8191][i%4]
        if state:row.update(port='D\t雪',bit=bit)
        contract['capture_requirements'].append(row)
        contract['logic_segments'].append(dict(id=segment,kind='rx_to_capture',fpga=row['fpga'],source_cut_net=f'net{cut}',capture_requirement=capture))
        contract['cut_nodes'][cut]['capture_segment_ids'].append(segment)
        expected.append([i,i+2,cut,part,int(state),endpoint,row.get('port',''),
                         bit.to_bytes(max(1,(bit.bit_length()+7)//8),'big').hex()if state else''])
    contract['metrics']=dict(transported_cut_nets=2,logic_segments=count+2,capture_requirements=count,dependency_edges=0)
    descriptor=write_phase3_exact_binary(binary,contract,['a','b'])
    with closing(sqlite3.connect(path))as db,db:
        db.execute('DELETE FROM cut_predecessors');db.execute('DELETE FROM cut_sinks');db.execute('DELETE FROM cuts')
        db.executemany('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)',[(i,f'net{i}',i,'combinational',1,None,0,0)for i in range(2)])
        db.executemany('UPDATE metadata SET value=? WHERE key=?',[(json.dumps(value),key)for key,value in dict(
            cut_records=2,semantic_contract_binary=descriptor,semantic_contract_sha256=descriptor['semantic_contract_sha256']).items()])
    result=subprocess.run([str(exact_semantic_view_test()),str(path),str(binary)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['captures']==sorted(expected,key=lambda row:(row[3],row[0]))


@pytest.mark.parametrize('damage',['checksum','unknown-id','wrong-cut','wrong-kind','zero-bit-length','oversized-bit','missing-endpoint-binding'])
def test_capture_sections_are_checked_against_the_complete_segment_population(source,tmp_path,damage):
    path,binary,descriptor,contract,_cuts=prepare(source,tmp_path,'transitions')
    if damage=='missing-endpoint-binding':
        from emuflow.phase3_exact_binary import write_phase3_exact_binary
        capture=dict(contract['capture_requirements'][0]);capture['id']=f"capture{len(contract['capture_requirements']):06d}"
        capture['endpoint']+='__additional';segment=f"segment{len(contract['logic_segments']):06d}"
        contract['capture_requirements'].append(capture)
        contract['logic_segments'].append(dict(id=segment,kind='rx_to_capture',fpga=capture['fpga'],
                                              source_cut_net=capture['cut_net'],capture_requirement=capture['id']))
        next(n for n in contract['cut_nodes']if n['net']==capture['cut_net'])['capture_segment_ids'].append(segment)
        contract['metrics']['logic_segments']+=1;contract['metrics']['capture_requirements']+=1
        with closing(sqlite3.connect(path))as db:
            parts=[row[0]for row in db.execute('SELECT fpga FROM parts ORDER BY part_index')]
        descriptor=write_phase3_exact_binary(binary,contract,parts)
        with closing(sqlite3.connect(path))as db,db:
            db.executemany('UPDATE metadata SET value=? WHERE key=?',[(json.dumps(value),key)for key,value in dict(
                semantic_contract_binary=descriptor,semantic_contract_sha256=descriptor['semantic_contract_sha256']).items()])
    data=binary.read_bytes();_magic,_version,offset,length=struct.unpack_from('<8sIQQ',data)
    manifest=json.loads(data[offset:offset+length]);payload=bytearray(data[:offset])
    records=[]
    for name,section in manifest['sections'].items():
        if not name.startswith('captures:'):continue
        position=section['offset']
        for _ in range(section['records']):
            record=position;kind=payload[position+8];position+=9
            position+=4+struct.unpack_from('<I',payload,position)[0]
            bit_position=None
            if kind:
                position+=4+struct.unpack_from('<I',payload,position)[0]
                bit_position=position;position+=4+struct.unpack_from('<I',payload,position)[0]
            records.append((section,record,bit_position))
    section,position,bit_position=next(row for row in records if row[2]is not None)if damage in {'zero-bit-length','oversized-bit'}else records[0]
    if damage=='checksum':payload[position+9+4]^=1
    elif damage=='unknown-id':struct.pack_into('<I',payload,position,2**32-1)
    elif damage=='wrong-cut':struct.pack_into('<I',payload,position+4,2**32-1)
    elif damage=='wrong-kind':payload[position+8]=2
    elif damage=='missing-endpoint-binding':
        # Keep record totals and every checksum valid while making one ID own
        # two capture segments and another none. Count-only checking misses it.
        segments=[s for n,s in manifest['sections'].items()if n.startswith('segments:')]
        captures=[(s,p)for s in segments for p in range(s['offset'],s['offset']+s['bytes'],14)if payload[p+4]==2]
        assert len(captures)>=2
        target=struct.unpack_from('<I',payload,captures[0][1]+10)[0]
        owner,record=captures[1];struct.pack_into('<I',payload,record+10,target)
        owner['sha256']=hashlib.sha256(payload[owner['offset']:owner['offset']+owner['bytes']]).hexdigest()
    else:
        struct.pack_into('<I',payload,bit_position,0 if damage=='zero-bit-length'else 1025)
    if damage!='checksum':section['sha256']=hashlib.sha256(payload[section['offset']:section['offset']+section['bytes']]).hexdigest()
    rewrite(path,binary,descriptor,manifest,payload)
    result=subprocess.run([str(exact_semantic_view_test()),str(path),str(binary)],capture_output=True,text=True)
    assert result.returncode!=0 and not result.stdout and result.stderr


@pytest.mark.parametrize('damage',['magic','truncated','checksum','manifest-seal','overlap','extra-section',
                                   'coordinate','missing-fanin','wrong-seal','changed-generation'])
def test_corrupt_or_mixed_semantics_are_rejected(source,tmp_path,damage):
    path,binary,descriptor,_contract,_cuts=prepare(source,tmp_path)
    data=bytearray(binary.read_bytes());_magic,_version,offset,length=struct.unpack_from('<8sIQQ',data)
    manifest=json.loads(data[offset:offset+length]);payload=data[:offset]
    args=[]
    if damage=='magic':data[0]^=1;binary.write_bytes(data)
    elif damage=='truncated':binary.write_bytes(data[:-1])
    elif damage=='manifest-seal':
        descriptor['manifest_sha256']='0'*64
        with closing(sqlite3.connect(path))as db, db:db.execute("UPDATE metadata SET value=? WHERE key='semantic_contract_binary'",(json.dumps(descriptor),))
    elif damage=='changed-generation':args=['mutate']
    elif damage=='checksum':
        section=next(s for name,s in manifest['sections'].items()if name.startswith('segments:')and s['records'])
        # Flip a valid stable flag to exercise the streaming checksum itself.
        payload[section['offset']+5]^=1
        binary.write_bytes(payload+data[offset:])
    else:
        if damage=='overlap':manifest['sections']['node_sources']['offset']=manifest['sections']['nets']['offset']
        elif damage=='extra-section':manifest['sections']['unknown']={**manifest['sections']['nets']}
        elif damage=='wrong-seal':manifest['semantic_contract_sha256']='0'*64
        elif damage=='coordinate':
            section=next(s for name,s in manifest['sections'].items()if name.startswith('segments:')and s['records'])
            struct.pack_into('<I',payload,section['offset']+6,2**32-2)
            section['sha256']=hashlib.sha256(payload[section['offset']:section['offset']+section['bytes']]).hexdigest()
        elif damage=='missing-fanin':
            section=manifest['sections']['node_sources'];struct.pack_into('<I',payload,section['offset'],0)
            section['sha256']=hashlib.sha256(payload[section['offset']:section['offset']+section['bytes']]).hexdigest()
        rewrite(path,binary,descriptor,manifest,payload)
    result=subprocess.run([str(exact_semantic_view_test()),str(path),str(binary),*args],capture_output=True,text=True)
    assert result.returncode!=0
    assert not result.stdout
    assert result.stderr
