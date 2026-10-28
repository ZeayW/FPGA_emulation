"""Every original member survives native rewrites and absent source tags."""
from contextlib import closing
import hashlib
import json
import sqlite3
import subprocess

import pytest

from emuflow.errors import EmuFlowError,ValidationError
from emuflow.original_lineage_index import index_original_lineage,_write_original_lineage_request
from emuflow.store_publication import bind_store_source
from tests.test_original_endpoint_index import native,runtime,prepared,original,build


@pytest.fixture
def observed(prepared):
    mapped=prepared[4]
    for name,path in mapped.items():
        with closing(sqlite3.connect(path)) as db,db:
            db.execute("ALTER TABLE cells ADD COLUMN type TEXT NOT NULL DEFAULT 'FDRE'")
            db.execute("UPDATE cells SET type='LUT2' WHERE id='gate'")
    index=build(prepared);partitions={}
    for name,path in mapped.items():
        lineage=path.with_name(name+'-lineage.sqlite')
        with closing(sqlite3.connect(lineage)) as db,db:
            db.executescript('PRAGMA application_id=0x454d564c;PRAGMA user_version=2;'
                             'CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);'
                             'CREATE TABLE objects(stage TEXT,name TEXT,ref TEXT,origin INTEGER,transformations TEXT);'
                             'CREATE INDEX objects_origin ON objects(stage,origin);')
            db.executemany('INSERT INTO metadata VALUES(?,?)',[
                ('schema','emuflow.vivado-cell-lineage/v2'),('retained_stage','optimized'),
                ('mapped_index_sha256',hashlib.sha256(path.read_bytes()).hexdigest())])
            rows=[('renamed','FDRE',0,'RETARGET')] if name=='fpga0' else [
                ('capture_rebuilt','FDRE',1,'RETARGET'),('capture_replica','FDSE',1,'REPLICATED')]
            db.executemany("INSERT INTO objects VALUES('optimized',?,?,?,?)",rows)
        partitions[name]=(path,lineage)
    return index,partitions,index.with_name('original-lineage.sqlite')


def run(observed,**kwargs):
    return index_original_lineage(*observed,stage='optimized',**kwargs)


def test_all_members_keep_source_collections_absence_and_changed_types(observed):
    out=run(observed)
    with closing(sqlite3.connect(out)) as db:
        metadata=dict(db.execute('SELECT * FROM metadata'))
        assert metadata['members']=='6' and metadata['copies']=='4' and metadata['observed_copies']=='2'
        assert metadata['changed_type_objects']=='1'
        assert metadata['physical_acceptance']==metadata['original_path_sta_acceptance']=='pending'
        assert db.execute('SELECT ordinal FROM members ORDER BY ordinal').fetchall()==[(i,)for i in range(6)]
        assert db.execute("SELECT count(*) FROM source_dispositions WHERE status='native-origin-unretained'").fetchone()==(2,)
        assert db.execute("SELECT observed_objects,changed_type_objects FROM source_dispositions WHERE mapped_cell_seq=1 AND part_index=1").fetchone()==(2,1)
        assert not any('delay' in row[1] or 'slack' in row[1]for row in db.execute('PRAGMA table_info(members)'))
        parents=json.loads(metadata['source_generations'])
        assert all('request.bin' not in p['path']for p in parents)
        assert all(len(p['generation'])==7 for p in parents)


@pytest.mark.parametrize('damage',['stage','mapped-digest','unknown-origin','partition-swap'])
def test_native_observation_owner_errors_preserve_previous_output(observed,damage):
    index,parts,out=observed;out.write_text('previous\n')
    if damage=='partition-swap':
        parts={'fpga0':parts['fpga1'],'fpga1':parts['fpga0']}
    else:
        with closing(sqlite3.connect(parts['fpga1'][1])) as db,db:
            if damage=='stage':db.execute("UPDATE metadata SET value='routed'WHERE key='retained_stage'")
            elif damage=='mapped-digest':db.execute("UPDATE metadata SET value=?WHERE key='mapped_index_sha256'",('0'*64,))
            else:db.execute("INSERT INTO objects VALUES('optimized','unowned','FDRE',99,'')")
    with pytest.raises(EmuFlowError):index_original_lineage(index,parts,out,stage='optimized')
    assert out.read_text()=='previous\n'


def test_original_historical_parent_generations_cannot_be_renewed(observed,prepared):
    index,parts,out=observed;out.write_text('previous\n')
    with closing(sqlite3.connect(prepared[2])) as db,db:db.execute("UPDATE pin_names SET name='changed'WHERE name='I1'")
    with pytest.raises(EmuFlowError,match='parent generation differs'):run(observed)
    assert out.read_text()=='previous\n'


def test_request_rejects_replaced_lineage_and_publication_aliases(observed,native):
    index,parts,out=observed;out.write_text('previous\n')
    paths=[index,*[p for pair in parts.values()for p in pair]]
    parents=tuple(bind_store_source(p,s)for p,s in zip(paths,['native-original-endpoint-index-v1',*(['xilinx-mapped-index-v2','vivado-lineage-store-v2']*2)]))
    request=out.with_name('request.bin');_write_original_lineage_request(request,parents=parents,partitions=tuple(parts),stage='optimized')
    with closing(sqlite3.connect(parts['fpga1'][1]))as db,db:db.execute("UPDATE objects SET name='replaced'WHERE origin=1")
    done=subprocess.run([str(native),'phase7','index-original-lineage',str(index),str(request),str(out),*[str(p)for p in paths[1:]]],text=True,capture_output=True)
    assert done.returncode and 'generation differs from its request'in done.stderr
    assert out.read_text()=='previous\n'
    with pytest.raises(ValidationError,match='aliases'):
        index_original_lineage(index,parts,index,stage='optimized')


def test_missing_mapped_copies_and_missing_partition_owners_remain_records(prepared):
    with closing(sqlite3.connect(prepared[4]['fpga1']))as db,db:db.execute("DELETE FROM cells WHERE id='gate'")
    # A new admitted source index records a missing mapped copy explicitly.
    observed_fixture=observed.__wrapped__(prepared)
    out=run(observed_fixture)
    with closing(sqlite3.connect(out))as db:
        assert db.execute("SELECT count(*) FROM source_dispositions WHERE status='mapped-copy-unavailable'").fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM members').fetchone()==(6,)


@pytest.mark.parametrize('reuse',[False,True])
def test_native_phase7c_member_ledger_keeps_every_original_without_numerical_placeholders(observed,prepared,reuse):
    from emuflow.native_word_phase7c import _original_members
    index,pairs,_=observed
    evidence={}
    for name,(mapped,lineage)in pairs.items():
        with closing(sqlite3.connect(lineage))as db,db:
            db.execute("UPDATE metadata SET value='routed'WHERE key='retained_stage'")
            db.execute("UPDATE objects SET stage='routed'")
        evidence[name]={'input_identity':{'mapped_index':{'path':str(mapped)}},'lineage':{'path':str(lineage)}}
    output=index.parent/'phase7c';output.mkdir()
    options={'endpoint_index':index}if reuse else dict(source_ir=prepared[2],path_database=prepared[0],assignment=prepared[3])
    result=_original_members(evidence,output,[],**options)
    assert result['status']=='complete-source-lineage-observations'and result['members']==6
    assert result['observed_copies']==2 and result['changed_type_objects']==1
    assert not any('delay'in k or'slack'in k for k in result)
