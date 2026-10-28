"""The post-route entry point observes native endpoints in a disposable view."""
import csv
import json
from pathlib import Path
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.native_word_observation import FILES,observe_native_word_checkpoint
from emuflow.vivado_session import _tcl_word
from tests.test_native_word_source_catalog import source,prepare
from tests.test_native_word_timing import MOCK
from tests.test_vivado_session import ROOT,TCL86


@pytest.mark.skipif(TCL86 is None,reason='Tcl 8.6 required')
@pytest.mark.parametrize('damage',[None,'duplicate','out-of-calendar','rx-target'])
def test_collector_preserves_nominal_checks_then_observes_every_tx_context(tmp_path,damage):
    roles=tmp_path/'roles';roles.mkdir();out=tmp_path/'output'
    (roles/'rx-roles.tsv').write_text('source_origin\ns7\ns9\n')
    query='payload[0]';rows=[f'{p}\t{query.encode().hex()}\n'for p in(0,2,4)]
    if damage=='duplicate':rows.append(rows[0])
    if damage=='out-of-calendar':rows.append('6\t'+query.encode().hex()+'\n')
    if damage=='rx-target':rows.append('1\t'+b'unused_rx[0]'.hex()+'\n')
    (roles/'targets.tsv').write_text('phase_word\tquery_hex\n'+''.join(rows))
    program=f'source {_tcl_word(ROOT/"scripts/vivado/native_word_observation.tcl")}\n'+MOCK+r'''
set events {}
proc set_param {args} {}
proc open_checkpoint {path} {lappend ::events open}
proc get_nets {args} {return {}}
proc emuflow_word_phase_bind {path} {return [dict create repeat_words 6]}
proc emuflow_word_phase_select {context phase} {lappend ::events phase:$phase}
proc emuflow_word_phase_discard {context} {lappend ::events close;unset ::emuflow_native_export_stage}
proc emuflow_load_fixed_io_map {path} {
 return [dict create {payload[0]} {word-tx serializer pad_p} {unused_rx[0]} {word-rx optimized_away unused_pad}]
}
proc emuflow_fixed_io_object {mapping logical} {
 if {$logical ne {payload[0]}} {error "unused RX hierarchy was queried"}
 return {native_serializer/D[3]}
}
proc emuflow_native_word_state_hold {sources stream} {lappend ::events hold}
proc emuflow_native_word_rx_infrastructure_hold {sources stream} {lappend ::events rxhold}
rename emuflow_native_word_reference original_reference
proc emuflow_native_word_reference {sources} {lappend ::events reference;original_reference $sources}
rename get_timing_paths original_timing_paths
proc get_timing_paths {args} {
 if {[lsearch -exact $args -delay_type]>=0} {lappend ::events control:[lindex $args [expr {[lsearch -exact $args -delay_type]+1}]]}
 return [original_timing_paths {*}$args]
}
'''
    program+=f'''
if {{[catch {{emuflow_observe_native_word snapshot {_tcl_word(roles)} boundaries {_tcl_word(out)} 2}} message]}} {{puts stderr $message;exit 2}}
puts $events
'''
    done=subprocess.run([TCL86],input=program,text=True,capture_output=True)
    if damage:
        assert done.returncode==2 and not(out/'native-observation-metrics.tsv').exists()
        assert 'context'in done.stderr
        return
    assert done.returncode==0 and not done.stderr
    assert done.stdout.splitlines()[-1]=='open hold rxhold control:max control:min reference phase:0 phase:2 phase:4 close'
    with(out/'native-observation-metrics.tsv').open(newline='')as stream:
        metrics={row['metric']:int(row['value'])for row in csv.DictReader(stream,delimiter='\t')}
    assert metrics==dict(dut_clocks=2,infrastructure_clocks=2,observed_rx_origins=1,
        requested_rx_origins=2,missing_rx_origins=1,repeat_words=6,capture_queries=20,tx_contexts=3)
    assert(out/'missing-rx-roles.tsv').read_text()=='source_origin\ns9\n'
    with(out/'fanin-summary.tsv').open(newline='')as stream:fanins=list(csv.DictReader(stream,delimiter='\t'))
    assert [int(row['phase_word'])for row in fanins]==[0,2,4]
    assert all(row['unclassified']=='1'and row['ports']=='2'for row in fanins)
    with(out/'native-control-checks.tsv').open(newline='')as stream:checks=list(csv.DictReader(stream,delimiter='\t'))
    assert all(float(row['slack_ns'])<0 for row in checks)


@pytest.fixture
def observation(source,monkeypatch):
    preparation=prepare(source);roles=Path(preparation['directory'])
    checkpoint=source.root/'routed.dcp';checkpoint.write_bytes(b'actual-native-checkpoint')
    boundary=source.root/'native-word-boundaries.tsv';boundary.write_text('canonical physical boundaries\n')
    def execute(executable,script,arguments,output,name):
        assert arguments==[str(checkpoint),str(roles),str(boundary),str(output),'2']
        assert script.name=='observe_native_word.tcl'
        for filename in FILES:(output/filename).write_text('header\n')
        (output/'native-observation-metrics.tsv').write_text('metric\tvalue\n'+''.join(f'{k}\t{v}\n'for k,v in dict(
            dut_clocks=2,infrastructure_clocks=3,observed_rx_origins=0,requested_rx_origins=1,
            missing_rx_origins=1,repeat_words=6,capture_queries=16,tx_contexts=3).items()))
        (output/'missing-rx-roles.tsv').write_text('source_origin\ns9\n')
        (output/'native-control-checks.tsv').write_text('delay_type\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex\n'+
            '\n'.join(f'{kind}\tnative-path\t{slack}\t1.2\t61\t62'for kind,slack in [('max',-.432),('min',.010)])+'\n')
        for filename,scope in [('native-dut-hold.tsv','dut-state-to-state'),('native-rx-infrastructure-hold.tsv','rx-to-infrastructure')]:
            (output/filename).write_text('scope\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex\n'+
                scope+'\tnative-no-timed-path\t\t\t\t\n')
        return dict(status='pass',log=str(output/name))
    monkeypatch.setattr('emuflow.vivado_physical.execute_vivado',execute)
    monkeypatch.setattr('emuflow.vivado_backend._resolve_vivado',lambda executable:'vivado')
    return source,roles,checkpoint,boundary,execute


def test_complete_observations_keep_real_timing_failure_and_missing_source_roles(observation):
    source,roles,checkpoint,boundary,_=observation;out=source.root/'observed'
    report=observe_native_word_checkpoint('fpga0',checkpoint,roles,boundary,out)
    assert report['status']=='complete'and report['fixed_clock_control_timing_met']is False
    assert report['counts']['missing_rx_origins']==1
    assert set(report['files'])==set(FILES)
    assert json.loads((out/'native-observation.json').read_text())==report
    assert checkpoint.read_bytes()==b'actual-native-checkpoint'
    with pytest.raises(ValidationError,match='fresh'):observe_native_word_checkpoint('fpga0',checkpoint,roles,boundary,out)


@pytest.mark.parametrize('damage',['changed-checkpoint','changed-role','incomplete-calendar','missing-file','invented-absent-cost'])
def test_partial_or_mixed_observations_never_publish_completion(observation,monkeypatch,damage):
    source,roles,checkpoint,boundary,execute=observation;out=source.root/'observed'
    def damaged(*args):
        result=execute(*args)
        if damage=='changed-checkpoint':checkpoint.write_bytes(b'other-native-snapshot')
        if damage=='changed-role':(roles/'rx-roles.tsv').write_text('source_origin\ns8\n')
        if damage=='incomplete-calendar':
            p=out/'native-observation-metrics.tsv';p.write_text(p.read_text().replace('tx_contexts\t3','tx_contexts\t2'))
        if damage=='missing-file':(out/'native-costs.tsv').unlink()
        if damage=='invented-absent-cost':
            p=out/'native-dut-hold.tsv';p.write_text(p.read_text().replace('native-no-timed-path\t\t','native-no-timed-path\t0.0\t'))
        return result
    monkeypatch.setattr('emuflow.vivado_physical.execute_vivado',damaged)
    with pytest.raises(ValidationError):observe_native_word_checkpoint('fpga0',checkpoint,roles,boundary,out)
    assert not(out/'native-observation.json').exists()
