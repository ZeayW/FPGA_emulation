"""All original source members survive mapped ownership and clock rewriting."""
from contextlib import closing
import hashlib,json,os,sqlite3,subprocess
from pathlib import Path
import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import EmuFlowError,ValidationError
from emuflow.original_endpoint_index import BASE_SCHEMAS,index_original_endpoints,_write_original_endpoint_request
from emuflow.store_publication import bind_store_source
from tests.native_build import emuflow_native
from tests.test_native_original_timing import original,PERIOD

@pytest.fixture(scope='module')
def native():
    supplied=os.environ.get('EMUFLOW_TEST_NATIVE_WORD_DRIVER')
    return Path(supplied)if supplied else emuflow_native()

@pytest.fixture(autouse=True)
def runtime(native,monkeypatch):monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native.parent.parent))

@pytest.fixture
def prepared(tmp_path,original):
    selector,payload,_=original
    value=dict(schema='emuflow.emuir/v1',design={'name':'tiny','top':'tiny'},warnings=[],clocks=[],
      ports=[{'id':n,'name':n,'direction':d,'width':1}for n,d in [('clk','input'),('data','input'),('data_out','output')]],
      instances=[dict(id=n,name=n,type=t,resources={'ff':1}if t=='FDRE'else{'lut':1},constant_connections=[])
                 for n,t in [('source','FDRE'),('capture','FDRE'),('gate','LUT2')]],nets=[])
    def endpoint(cell,port):return dict(instance=cell,port=port,bit=0)
    for name,drivers,sinks in [
      ('clk',[endpoint(None,'clk')],[endpoint('source','C'),endpoint('gate','I0')]),
      ('data',[endpoint(None,'data')],[endpoint('source','D')]),
      ('q',[endpoint('source','Q')],[endpoint('capture','D'),endpoint('gate','I1'),endpoint(None,'data_out')]),
      ('gclk',[endpoint('gate','O')],[endpoint('capture','C')])]:
        value['nets'].append(dict(id=name,name=name,aliases=[],drivers=drivers,sinks=sinks,fanout=len(sinks),cut_class='register_output'))
    source=tmp_path/'source.sqlite';write_compact_emuir(source,value)
    with closing(sqlite3.connect(payload))as db,db:
        db.execute("UPDATE paths SET end_object='gate/I1',end_instance='gate',end_port='I1'WHERE ordinal=2")
    desc=json.loads(selector.read_text());desc['payloads']['paths']['sha256']=hashlib.sha256(payload.read_bytes()).hexdigest();selector.write_text(json.dumps(desc))
    assignment=tmp_path/'assignment.sqlite'
    with closing(sqlite3.connect(assignment))as db,db:
        db.executescript('''PRAGMA application_id=0x454d5541;PRAGMA user_version=4;
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT);
          CREATE TABLE copies(source_instance_seq INTEGER,part_index INTEGER,copy_id TEXT,is_replica INTEGER);''')
        db.executemany('INSERT INTO metadata VALUES(?,?)',[('schema',json.dumps('emuflow.phase3-assignment-sqlite/v4')),('source_instance_count','3')])
        db.executemany('INSERT INTO parts VALUES(?,?)',[(0,'fpga0'),(1,'fpga1')])
        # Assignment/source sequence numbers differ; IDs supply the bridge.
        db.executemany('INSERT INTO copies VALUES(?,?,?,?)',[(0,1,'gate',0),(1,0,'source',0),(1,1,'source_copy',1),(2,1,'capture',0)])
    mapped={}
    for fpga,names in [('fpga0',['source']),('fpga1',['gate','capture','source_copy'])]:
        path=tmp_path/(fpga+'.sqlite');mapped[fpga]=path
        with closing(sqlite3.connect(path))as db,db:
            db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);CREATE TABLE cells(seq INTEGER PRIMARY KEY,id TEXT UNIQUE);')
            db.execute('INSERT INTO metadata VALUES(?,?)',('schema','emuflow.xilinx-mapped-index/v2'))
            db.executemany('INSERT INTO cells VALUES(?,?)',enumerate(names))
    return selector,payload,source,assignment,mapped,tmp_path/'endpoint-index.sqlite'


def build(prepared,**kwargs):
    selector,payload,source,assignment,mapped,out=prepared
    return index_original_endpoints(selector,payload,source,assignment,mapped,out,
        selector_sha256=hashlib.sha256(selector.read_bytes()).hexdigest(),source_context={'scope':'source endpoint fixture'},**kwargs)


def test_all_members_keep_source_coordinates_nonstate_checks_and_complete_budgets(prepared):
    out=build(prepared)
    with closing(sqlite3.connect(out))as db:
        meta=dict(db.execute('SELECT * FROM metadata'))
        assert meta['members']=='6' and meta['state_members']=='4' and meta['cell_input_members']=='1' and meta['output_members']=='1'
        assert meta['copies']=='4' and meta['missing_mapped_copies']=='0'
        assert meta['physical_acceptance']==meta['original_path_sta_acceptance']=='pending'
        assert db.execute('SELECT ordinal FROM members ORDER BY ordinal').fetchall()==[(i,)for i in range(6)]
        assert db.execute('SELECT e.source_kind FROM members m JOIN endpoints e ON e.seq=m.end_endpoint WHERE m.ordinal=2').fetchone()==('cell-input',)
        assert db.execute('SELECT source_cell_seq,assignment_cell_seq,part_index,mapped_cell_seq FROM cell_owners WHERE is_replica=1').fetchone()==(0,1,1,2)
        records=[json.loads(row[0])for row in db.execute('SELECT record_json FROM budgets')]
        assert any(b['source_period_ns']==PERIOD and b['launch_edge_ns']==PERIOD/2 for b in records)
        assert not any('delay'in row[1]for row in db.execute('PRAGMA table_info(members)'))


def test_missing_mapped_copy_is_recorded_without_claiming_elimination(prepared):
    with closing(sqlite3.connect(prepared[4]['fpga1']))as db,db:db.execute("DELETE FROM cells WHERE id='gate'")
    out=build(prepared)
    with closing(sqlite3.connect(out))as db:
        assert db.execute("SELECT value FROM metadata WHERE key='missing_mapped_copies'").fetchone()==('1',)
        assert db.execute('SELECT count(*) FROM members').fetchone()==(6,)
        assert db.execute('SELECT count(*) FROM cell_owners WHERE mapped_cell_seq IS NULL').fetchone()==(1,)


@pytest.mark.parametrize('damage',['source-endpoint','assignment-source','selector-seal'])
def test_invalid_source_relations_preserve_the_previous_output(prepared,damage):
    selector,payload,source,assignment,mapped,out=prepared;out.write_text('previous\n')
    if damage=='source-endpoint':
        with closing(sqlite3.connect(payload))as db,db:db.execute("UPDATE paths SET end_port='unknown' WHERE ordinal=0")
    elif damage=='assignment-source':
        with closing(sqlite3.connect(assignment))as db,db:db.execute("UPDATE copies SET copy_id='unknown'WHERE source_instance_seq=0")
    else:
        with pytest.raises(EmuFlowError):
            index_original_endpoints(selector,payload,source,assignment,mapped,out,selector_sha256='0'*64,source_context={})
        assert out.read_text()=='previous\n';return
    with pytest.raises(EmuFlowError):build(prepared)
    assert out.read_text()=='previous\n'


def test_control_receipts_keep_original_generations_and_output_cannot_alias_sources(prepared):
    selector,payload,source,assignment,mapped,out=prepared
    control=out.parent/'control.json';control.write_text('{}')
    parent=bind_store_source(control,'physical-input-document-v1')
    build(prepared,control_sources=[parent])
    with closing(sqlite3.connect(out))as db:
        refs=json.loads(db.execute("SELECT value FROM metadata WHERE key='source_generations'").fetchone()[0])
        assert refs[-1]['path']==str(control) and refs[-1]['generation']==list(parent.generation)
        assert all(Path(ref['path']).exists()for ref in refs)
    previous=out.read_bytes();control.write_text('{"changed":true}')
    with pytest.raises(ValidationError,match='generation'):build(prepared,control_sources=[parent])
    assert out.read_bytes()==previous
    with pytest.raises(ValidationError,match='aliases'):
        index_original_endpoints(selector,payload,source,assignment,mapped,source,
            selector_sha256=hashlib.sha256(selector.read_bytes()).hexdigest(),source_context={})


def test_request_rejects_replaced_source_before_publication(prepared,native):
    selector,payload,source,assignment,mapped,out=prepared
    paths=[selector,payload,source,assignment,*mapped.values()]
    parents=[bind_store_source(p,s)for p,s in zip(paths,[*BASE_SCHEMAS,'xilinx-mapped-index-v2','xilinx-mapped-index-v2'])]
    request=out.parent/'request.bin';_write_original_endpoint_request(request,parents=parents,partitions=tuple(mapped),
        selector_sha256=hashlib.sha256(selector.read_bytes()).hexdigest(),source_context='{}',control_schemas=[])
    with closing(sqlite3.connect(mapped['fpga0']))as db,db:db.execute("UPDATE cells SET id='changed'")
    out.write_text('previous\n')
    done=subprocess.run([str(native),'phase7','index-original-endpoints',*(str(p)for p in paths[:4]),str(request),str(out),
        *(str(p)for p in paths[4:])],capture_output=True,text=True)
    assert done.returncode!=0 and 'generation differs'in done.stderr
    assert out.read_text()=='previous\n'
