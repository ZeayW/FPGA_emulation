"""Canonical transport source roles include receive-only counter periods."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from emuflow.errors import ValidationError
from emuflow.native_word_source_catalog import prepare_native_word_sources,write_native_word_index_from_sources
from emuflow.store_publication import bind_store_source


class View(dict):
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def bound_store_sources(self):return self.parents


@pytest.fixture
def source(tmp_path,monkeypatch):
    def generation(path):
        s=Path(path).stat()
        return(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns//10**9,s.st_mtime_ns%10**9,s.st_ctime_ns//10**9,s.st_ctime_ns%10**9)
    monkeypatch.setattr('emuflow.store_publication.native_file_generation',generation)
    schedule_path=tmp_path/'schedule.json';schedule_path.write_text('{}\n')
    entries=[SimpleNamespace(entry_id='ab',demand='a',net='n0',link='link01',source='fpga0',sink='fpga1',slot=0,arrival_slot=6,lane=0,tdm_ratio=16),
             SimpleNamespace(entry_id='ba',demand='b',net='n1',link='link10',source='fpga1',sink='fpga0',slot=1,arrival_slot=7,lane=0,tdm_ratio=24)]
    schedule=View(word_event_policy='periodic-lvds-8bit-word-load-to-rx-visible-v2')
    schedule.parents=(bind_store_source(schedule_path,'tdm-schedule-selector-v4'),)
    schedule.iter_transport_records=lambda:iter(entries)
    monkeypatch.setattr('emuflow.tdm_schedule_storage.read_tdm_schedule',lambda path:schedule)
    mapped={};boundaries={};views={}
    for fpga in('fpga0','fpga1'):
        path=tmp_path/(fpga+'.sqlite');mapped[fpga]=path
        with closing(sqlite3.connect(path))as db,db:
            db.executescript('CREATE TABLE cells(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,type TEXT,record_json TEXT);'
              'CREATE TABLE netnames(id TEXT PRIMARY KEY,record_json TEXT);'
              'CREATE TABLE cell_pins(cell_seq INTEGER,port TEXT,port_bit INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER);')
            for seq,name,net in[(9,'rx_shadow',200),(10,'phase2',100),(20,'phase3_0',101),(21,'phase3_1',102),(31,'phase4_1',103)]:
                db.execute('INSERT INTO cells VALUES(?,?,?,?)',(seq,name,'FDRE',json.dumps({'parameters':{'IS_C_INVERTED':'0'}})))
                db.executemany('INSERT INTO cell_pins VALUES(?,?,?,?,?,?)',[(seq,'C',0,0,0,2),(seq,'Q',0,1,0,net)])
            for period,values in[(1,['0']),(2,[100]),(3,[101,102]),(4,[100,103])]:
                for bit,value in enumerate(values):db.execute('INSERT INTO netnames VALUES(?,?)',(f'__emuflow_transport__/payload_phase_{period}[{bit}]',json.dumps({'bits':[value]})))
        boundary_path=tmp_path/(fpga+'-boundary.json');boundaries[fpga]=boundary_path;boundary_path.write_text('{}\n')
        endpoints=[]
        for item in entries:
            tx=item.source==fpga
            endpoints.append(dict(id=item.entry_id+('-tx'if tx else'-rx'),kind='tx'if tx else'rx',schedule_entry=item.entry_id,
                demand=item.demand,logical_net=item.net,link=item.link,peer=item.sink if tx else item.source,lane=item.lane,source_class='dut-state',
                merged_ir=dict(boundary_register_instances=[]if tx else['rx_shadow'],external_port='tx_'+item.entry_id if tx else'rx_'+item.entry_id,external_port_bit=0)))
        view=View(fpga=fpga,endpoints=endpoints);view.parents=(bind_store_source(boundary_path,'physical-input-document-v1'),);views[boundary_path]=view
    monkeypatch.setattr('emuflow.boundary_identity_storage.read_boundary_identity_storage',lambda path:views[Path(path)])
    class Mapped:
        ports={'fabric_clk':{'direction':'input','bits':[2]}}
        def __init__(self,path):self.connection=sqlite3.connect(path)
        def __enter__(self):return self
        def __exit__(self,*_):self.connection.close()
    monkeypatch.setattr('emuflow.xilinx_netlist.open_xilinx_mapped_index',Mapped)
    return SimpleNamespace(root=tmp_path,schedule_path=schedule_path,schedule=schedule,entries=entries,mapped=mapped,boundaries=boundaries,views=views)


def prepare(source,fpga='fpga0',**options):
    return prepare_native_word_sources(fpga,source.schedule_path,source.boundaries[fpga],source.mapped[fpga],source.root/(fpga+'-sources'),**options)


@pytest.mark.parametrize('naming',['indexed','scalar'])
def test_receive_only_periods_join_tx_calendar_and_source_roles(source,naming):
    if naming=='scalar':
        with closing(sqlite3.connect(source.mapped['fpga0']))as db,db:
            db.execute("UPDATE netnames SET id='__emuflow_transport__/payload_phase_2'WHERE id='__emuflow_transport__/payload_phase_2[0]'")
    result=prepare(source);out=Path(result['directory'])
    assert result['calendar_periods']==[2,3]and result['repeat_words']==6 and result['tx_contexts']==3
    assert(out/'counter-roles.tsv').read_text()=='period\tbit\tsource_origin\n2\t0\ts10\n3\t0\ts20\n3\t1\ts21\n'
    assert(out/'rx-roles.tsv').read_text()=='source_origin\ns9\n'
    assert(out/'targets.tsv').read_text()=='phase_word\tquery_hex\n'+''.join(f'{p}\t'+b'tx_ab[0]'.hex()+'\n'for p in(0,2,4))
    with closing(sqlite3.connect(out/'catalog.sqlite'))as db:
        metadata={k:json.loads(v)for k,v in db.execute('SELECT * FROM metadata')}
        assert metadata['fixed_latency_words']==6 and len(metadata['source_generations'])==3


@pytest.mark.parametrize('damage',['wrong-policy','wrong-latency','bad-ratio','unsupported-native-period','boundary-owner','counter-inversion','counter-clock','counter-missing','incompatible-alias'])
def test_invalid_protocol_roles_leave_no_published_source_directory(source,damage):
    if damage=='wrong-policy':source.schedule['word_event_policy']='old-epoch'
    if damage=='wrong-latency':source.entries[0].arrival_slot=7
    if damage=='bad-ratio':source.entries[0].tdm_ratio=9
    if damage=='unsupported-native-period':source.entries[0].tdm_ratio=264
    if damage=='boundary-owner':source.views[source.boundaries['fpga0']]['fpga']='fpga9'
    with closing(sqlite3.connect(source.mapped['fpga0']))as db,db:
        if damage=='counter-inversion':db.execute("UPDATE cells SET record_json=?WHERE seq=10",(json.dumps({'parameters':{'IS_C_INVERTED':'1'}}),))
        if damage=='counter-clock':db.execute("UPDATE cell_pins SET net_bit=3 WHERE cell_seq=10 AND port='C'")
        if damage=='counter-missing':db.execute("DELETE FROM netnames WHERE id='__emuflow_transport__/payload_phase_3[1]'")
        if damage=='incompatible-alias':db.execute("UPDATE netnames SET record_json=?WHERE id='__emuflow_transport__/payload_phase_3[0]'",(json.dumps({'bits':[100]}),))
    with pytest.raises(ValidationError):prepare(source)
    assert not(source.root/'fpga0-sources').exists()


def test_compatible_shared_counter_bit_and_unit_period(source):
    source.entries[1].tdm_ratio=32
    result=prepare(source)
    text=(Path(result['directory'])/'counter-roles.tsv').read_text()
    assert '2\t0\ts10\n'in text and '4\t0\ts10\n'in text
    # A separate partition directory can be prepared after the protocol changes.
    for item in source.entries:item.tdm_ratio=8
    with closing(sqlite3.connect(source.mapped['fpga1']))as db,db:
        db.execute("UPDATE netnames SET id='__emuflow_transport__/payload_phase_1'WHERE id='__emuflow_transport__/payload_phase_1[0]'")
    result=prepare(source,'fpga1')
    assert result['repeat_words']==1
    assert(Path(result['directory'])/'counter-roles.tsv').read_text()=='period\tbit\tsource_origin\n1\t0\tconstant:0\n'


@pytest.mark.parametrize('option',[{'maximum_repeat_words':2},{'maximum_contexts':2}])
def test_execution_bounds_are_checked_before_source_publication(source,option):
    with pytest.raises(ValidationError,match='bound'):prepare(source,**option)
    assert not(source.root/'fpga0-sources').exists()


def test_both_canonical_owners_and_explicit_clock_windows_reach_native_index(source,monkeypatch):
    directories={fpga:prepare(source,fpga)['directory']for fpga in source.mapped}
    clocks={fpga:{'native_core':'core'}for fpga in directories}
    windows=[('core',a,'core',b,100000 if a==b else 50000)for a in(1,2)for b in(1,2)]
    seen={}
    def publish(path,**kwargs):seen.update(kwargs);seen['transfers']=list(kwargs['transfers']);return path
    monkeypatch.setattr('emuflow.native_word_analysis.write_native_word_index',publish)
    write_native_word_index_from_sources(directories,clocks,windows,source.root/'network.sqlite',source_context={'mode':'fixture'})
    assert seen['transfers']==[('ab','fpga0','fpga1',0,16,6),('ba','fpga1','fpga0',1,24,6)]
    assert seen['clock_windows']==windows
    assert all(p['calendar_periods']==[2,3]for p in seen['partitions'])
    assert len(seen['source_bindings'])==15  # one schedule, two mapped/boundaries, ten closed source files


@pytest.mark.parametrize('damage',['missing-peer','role-file','old-source'])
def test_source_network_rejects_incomplete_or_changed_receipts(source,monkeypatch,damage):
    directories={fpga:prepare(source,fpga)['directory']for fpga in source.mapped}
    if damage=='missing-peer':directories.pop('fpga1')
    elif damage=='role-file':(Path(directories['fpga0'])/'rx-roles.tsv').write_text('source_origin\ns99\n')
    else:
        with closing(sqlite3.connect(source.mapped['fpga0']))as db,db:db.execute("DELETE FROM cells WHERE seq=9")
    def publish(*args,**kwargs):raise AssertionError('invalid source index was published')
    monkeypatch.setattr('emuflow.native_word_analysis.write_native_word_index',publish)
    with pytest.raises(ValidationError):
        write_native_word_index_from_sources(directories,{p:{'native':'core'}for p in directories},[],source.root/'network.sqlite',source_context={})


def test_clock_context_receipts_keep_original_generation_and_five_ns_scale(source,monkeypatch):
    directories={fpga:prepare(source,fpga)['directory']for fpga in source.mapped}
    clocks={p:{'native':'core'}for p in directories}
    control=source.root/'clocks.tcl';control.write_text('create_clock -period 100\n')
    parent=bind_store_source(control,'opensta-timing-input-v1');seen=[]
    def publish(path,**kwargs):seen.extend(kwargs['source_bindings']);return path
    monkeypatch.setattr('emuflow.native_word_analysis.write_native_word_index',publish)
    arguments=dict(source_context={},source_bindings=(parent,))
    write_native_word_index_from_sources(directories,clocks,[],source.root/'network.sqlite',**arguments)
    assert parent in seen
    with pytest.raises(ValidationError,match='five-nanosecond'):
        write_native_word_index_from_sources(directories,clocks,[],source.root/'network.sqlite',word_period_ps=4000,**arguments)
    control.write_text('create_clock -period 200\n')
    with pytest.raises(ValidationError,match='generation changed'):
        write_native_word_index_from_sources(directories,clocks,[],source.root/'network.sqlite',**arguments)
