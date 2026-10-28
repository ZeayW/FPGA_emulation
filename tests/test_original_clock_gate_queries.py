"""Gate-check projection follows local RX drivers and observes native replicas."""
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.original_clock_gate_queries import write_original_clock_gate_queries, HEADER
from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT, TCL86


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    def generation(path):
        s = Path(path).stat()
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns//10**9,
                s.st_mtime_ns%10**9, s.st_ctime_ns//10**9, s.st_ctime_ns%10**9)
    monkeypatch.setattr('emuflow.store_publication.native_file_generation', generation)
    source = tmp_path/'source.sqlite'
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript('CREATE TABLE pin_names(seq INTEGER PRIMARY KEY,name TEXT);INSERT INTO pin_names VALUES(0,\'I1\');')
    mapped = {}
    for fpga in ('fpga0', 'fpga1'):
        path = tmp_path/(fpga+'.sqlite'); mapped[fpga] = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE cells(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,type TEXT,record_json TEXT);'
                             'CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port TEXT,port_bit INTEGER,port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER);')
            db.execute('INSERT INTO cells VALUES(?,?,?,?)', (7, 'gate', 'LUT2', json.dumps({'parameters': {'INIT': '1000'}})))
            # The enable register on fpga0 is remote. The destination's local
            # input driver is RX origin s90, not source-enable origin s40.
            db.execute('INSERT INTO cells VALUES(?,?,?,?)', (40 if fpga=='fpga0' else 90, 'enable' if fpga=='fpga0' else 'rx_shadow', 'FDRE', '{}'))
            db.execute('INSERT INTO cell_pins VALUES(7,\'gate\',\'I1\',0,1,0,0,12)')
            db.execute('INSERT INTO cell_pins VALUES(?,?,\'Q\',0,1,1,0,12)', (40 if fpga=='fpga0' else 90, 'enable' if fpga=='fpga0' else 'rx_shadow'))
    index = tmp_path/'index.sqlite'
    parents = [dict(path=str(p), schema=s, generation=generation(p)) for p,s in
               [(source,'emuir-compact-v2'),*( (p,'xilinx-mapped-index-v2') for p in mapped.values())]]
    with closing(sqlite3.connect(index)) as db, db:
        db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE parts(part_index INTEGER,fpga TEXT);'
                         'CREATE TABLE endpoints(seq INTEGER PRIMARY KEY,role INTEGER,source_cell_seq INTEGER,source_port_seq INTEGER,bit INTEGER,source_kind TEXT);'
                         'CREATE TABLE cell_owners(seq INTEGER,source_cell_seq INTEGER,part_index INTEGER,mapped_cell_seq INTEGER);')
        db.executemany('INSERT INTO metadata VALUES(?,?)', [('schema','emuflow.native-original-endpoint-index/v1'),('source_generations',json.dumps(parents))])
        db.executemany('INSERT INTO parts VALUES(?,?)', [(0,'fpga0'),(1,'fpga1')])
        db.execute("INSERT INTO endpoints VALUES(5,1,100,0,0,'cell-input')")
        db.execute('INSERT INTO cell_owners VALUES(0,100,1,7)')
    return index, source, mapped, tmp_path/'requests'


def test_cross_gate_uses_destination_local_rx_origin(prepared):
    result = write_original_clock_gate_queries(*prepared)
    assert result['endpoints']==result['queries']==1
    entry = result['catalog'][0]
    assert (entry['fpga'],entry['driver_origin'],entry['driver_pin'])==('fpga1',90,'Q')
    with (prepared[-1]/'fpga1.tsv').open() as f:
        assert list(csv.reader(f, delimiter='\t'))==[list(HEADER),['5','s7',b'I1'.hex(),'s90',b'Q'.hex()]]
    assert result['physical_acceptance']==result['original_path_sta_acceptance']=='pending'


@pytest.mark.parametrize('damage,status', [('shape','unsupported-source-shape'),('constant','non-net-enable'),
                                         ('multiple','non-unique-local-enable-driver'),('missing','missing-mapped-copy')])
def test_unresolved_shapes_remain_explicit_without_false_elimination(prepared, damage, status):
    index, source, mapped, out = prepared
    with closing(sqlite3.connect(mapped['fpga1'])) as db, db:
        if damage=='shape': db.execute("UPDATE cells SET type='LUT3' WHERE seq=7")
        if damage=='constant': db.execute("UPDATE cell_pins SET value_kind=1 WHERE cell_seq=7")
        if damage=='multiple': db.execute("INSERT INTO cell_pins VALUES(99,'other','Q',0,1,1,0,12)")
    if damage=='missing':
        with closing(sqlite3.connect(index)) as db, db: db.execute('UPDATE cell_owners SET mapped_cell_seq=NULL')
    # Bind this new test generation as the admitted original-index parent.
    from emuflow.store_publication import bind_store_source
    with closing(sqlite3.connect(index)) as db, db:
        parents=json.loads(db.execute("SELECT value FROM metadata WHERE key='source_generations'").fetchone()[0])
        for p in parents: p['generation']=list(bind_store_source(Path(p['path']),p['schema']).generation)
        db.execute("UPDATE metadata SET value=? WHERE key='source_generations'",(json.dumps(parents),))
    result=write_original_clock_gate_queries(*prepared)
    assert result['queries']==0 and result['catalog'][0]['status']==status
    assert 'eliminated' not in json.dumps(result)


def test_changed_or_exchanged_parents_are_rejected_without_output(prepared):
    index, source, mapped, output = prepared
    with pytest.raises(ValidationError,match='owners differ'):
        write_original_clock_gate_queries(index,source,dict(fpga0=mapped['fpga1'],fpga1=mapped['fpga0']),output)
    assert not output.exists()
    with closing(sqlite3.connect(mapped['fpga1'])) as db, db: db.execute('DELETE FROM cells WHERE seq=90')
    with pytest.raises(ValidationError,match='generation changed'): write_original_clock_gate_queries(*prepared)
    assert not output.exists()


@pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')
@pytest.mark.parametrize('mode',['converted','gate-present','driver-missing','pin-missing','no-fanout'])
def test_native_observer_retains_replicas_ce_checks_and_unresolved_context(tmp_path,mode):
    request, output=tmp_path/'query.tsv', tmp_path/'observed.tsv'
    request.write_text('\t'.join(HEADER)+'\n5\ts7\t4931\ts90\t51\n')
    program='set mode '+_tcl_word(mode)+'\n'+r'''
proc get_cells {args} {
 if {[lsearch -exact $args -hier]>=0} {
  if {$::mode eq "driver-missing"} {return {}}
  if {$::mode eq "gate-present"} {return {gate renamed rx_copy}}
  return {renamed rx_copy}
 }
 set name [lindex $args end]
 return [string range $name 0 [expr {[string last / $name]-1}]]
}
proc get_pins {args} {
 set owner [lindex $args [expr {[lsearch -exact $args -of_objects]+1}]]
 if {[lsearch -exact $args -filter]>=0} {return $owner/C}
 if {$owner eq "gate"} {return gate/I1}
 if {$::mode eq "pin-missing"} {return $owner/O}
 return $owner/Q
}
proc get_clocks {args} {return emuflow_dut_clk_0}
proc get_property {args} {
 lassign [lrange $args end-1 end] key obj
 switch $key {
  NAME {return $obj}
  EMUFLOW_SOURCE {
   set result {}
   foreach c $obj {lappend result [expr {$c eq "gate" ? "s7" : $c in {renamed rx_copy} ? "s90" : "s100"}]}
   return $result
  }
  REF_NAME {return [expr {$obj eq "gate" ? "LUT2" : $obj eq "capture_copy" ? "FDSE" : "FDRE"}]}
  REF_PIN_NAME {return [lindex [split $obj /] end]}
  CLASS {return [expr {$obj eq "external" ? "port" : "pin"}]}
 }
 error "unexpected property $key"
}
proc all_fanout {args} {
 if {[lindex $args end] ne {renamed/Q rx_copy/Q}} {error "source replica dropped"}
 if {[lsearch -exact $args -trace_arcs]<0} {error "arc scope unspecified"}
 if {$::mode eq "no-fanout"} {return {}}
 return {capture/CE capture_copy/CE other/D external}
}
proc set_property {args} {error "physical design modified"}
proc set_false_path {args} {error "timing path hidden"}
proc set_max_delay {args} {error "timing constraint modified"}
'''
    program+='source '+_tcl_word(ROOT/'scripts/vivado/original_clock_gate_observations.tcl')+'\n'
    program+='if {[catch {emuflow_original_gate_observations '+_tcl_word(request)+' '+_tcl_word(output)+'} message]} {puts stderr $message;exit 2}\n'
    done=subprocess.run([TCL86],input=program,text=True,capture_output=True)
    assert done.returncode==0 and not done.stderr, done.stderr
    with output.open() as f: rows=list(csv.DictReader(f,delimiter='\t'))
    states={r['projection'] for r in rows}
    assert ('source-pin-observed' in states)==(mode=='gate-present')
    if mode=='driver-missing': assert 'local-driver-unretained' in states
    elif mode=='pin-missing': assert 'local-driver-pin-unretained' in states
    elif mode=='no-fanout': assert 'no-native-endpoint-observed' in states
    else:
        ce=[r for r in rows if r['projection']=='enable-fanout-ce-candidate']
        assert {r['native_ref'] for r in ce}=={'FDRE','FDSE'}
        assert len(ce)==2 and 'non-ce-fanout-context' in states
    assert not any('delay' in key or 'slack' in key for key in rows[0])
