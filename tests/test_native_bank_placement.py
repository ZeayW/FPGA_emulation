"""Bank placement follows source transport roles and native device geometry."""
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.lvds_word_plan import WORD_PHY_PLAN_SCHEMA
from emuflow.native_bank_placement import POLICY, read_native_bank_placement_request, write_native_bank_placement_request
from emuflow.tdm_word import WORD_EVENT_POLICY
from emuflow.vivado_session import _tcl_word
from tests.test_native_word_source_catalog import source, prepare
from tests.test_vivado_session import ROOT, TCL86


def request(source, banks=None):
    prepared = prepare(source)
    directory = Path(prepared['directory'])
    plan = source.root / 'word-plan.json'
    if banks is None:
        banks = [dict(bank=19, direction='rx', link='link10', peer='fpga1', logical_lsb=0),
                 dict(bank=20, direction='tx', link='link01', peer='fpga1', logical_lsb=0)]
    plan.write_text(json.dumps(dict(schema=WORD_PHY_PLAN_SCHEMA, status='pass',
        word_event_policy=WORD_EVENT_POLICY, fpgas=[dict(fpga='fpga0', banks=banks)])))
    return plan, directory, source.root / 'bank-placement'


@pytest.mark.parametrize('bank_identifier', [str, int])
def test_canonical_rx_entry_selects_its_link_peer_and_lane_slice(source, bank_identifier):
    plan, directory, output = request(source)
    document = json.loads(plan.read_text())
    for bank in document['fpgas'][0]['banks']:
        bank['bank'] = bank_identifier(bank['bank'])
    plan.write_text(json.dumps(document))
    result = write_native_bank_placement_request('fpga0', plan, directory, output)
    assert result['policy'] == POLICY and result['banks'] == 2
    assert result['rx_projections'] == 1
    assert (output / 'rx-bank-roles.tsv').read_text() == 'source_origin\tbank\ns9\t19\n'
    assert (output / 'banks.tsv').read_text() == (
        'bank\tdirection\tinstance\n19\trx\tnative_bank19_rx\n20\ttx\tnative_bank20_tx\n')
    for name, reference in result['files'].items():
        assert reference['sha256'] == file_sha256(output / name)
    assert json.loads((output / 'request.json').read_text()) == result
    assert read_native_bank_placement_request(output / 'request.json', 'fpga0')[0] == result
    (output / 'banks.tsv').write_text('changed placement table\n')
    with pytest.raises(ValidationError, match='source changed'):
        read_native_bank_placement_request(output / 'request.json', 'fpga0')


@pytest.mark.parametrize('damage', ['peer', 'link', 'slice', 'duplicate-bank', 'stale-catalog'])
def test_incompatible_source_projection_is_rejected_before_publication(source, damage):
    plan, directory, output = request(source)
    data = json.loads(plan.read_text())
    bank = data['fpgas'][0]['banks'][0]
    if damage in ('peer', 'link'):
        bank[damage] = 'different'
    if damage == 'slice':
        bank['logical_lsb'] = 160
    if damage == 'duplicate-bank':
        data['fpgas'][0]['banks'][1]['bank'] = 19
    if damage == 'stale-catalog':
        with sqlite3.connect(directory / 'catalog.sqlite') as db:
            db.execute("UPDATE entries SET lane=1 WHERE id='ba'")
    plan.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        write_native_bank_placement_request('fpga0', plan, directory, output)
    assert not output.exists()


@pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')
def test_native_geometry_groups_bank_logic_and_rx_without_preserving_optimized_objects(tmp_path):
    placement = tmp_path / 'request'; placement.mkdir()
    (placement / 'banks.tsv').write_text('bank\tdirection\tinstance\n19\trx\tnative_bank19_rx\n29\trx\tnative_bank29_rx\n')
    (placement / 'rx-bank-roles.tsv').write_text('source_origin\tbank\ns1\t19\ns2\t29\ns3\t19\ns4\t29\n')
    output = tmp_path / 'output'; output.mkdir()
    script = 'source ' + _tcl_word(ROOT / 'scripts/vivado/native_bank_placement.tcl') + '\n'
    script += r'''
set prefix mapped_partition/__emuflow_transport__
set shared $prefix/shared
set shadow $prefix/shadow
set counter $prefix/counter
set refs [dict create native_bank19_rx/local LUT4 native_bank19_rx/phy RX_BITSLICE native_bank19_rx/fixed LUT6 native_bank29_rx/local FDRE $shared FDRE $shadow FDRE $counter FDRE]
proc get_property {args} {
  set property [lindex $args end-1];set object [lindex $args end]
  if {$property eq "NAME"} {return $object}
  if {$property eq "REF_NAME"} {return [dict get $::refs $object]}
  if {$property eq "IS_LOC_FIXED"} {return [expr {$object eq "native_bank19_rx/fixed"}]}
  if {$property eq "EMUFLOW_SOURCE"} {
    if {$object eq $::shared} {return s1}
    if {$object eq $::shadow} {return s3}
    return s9
  }
  error "unexpected property: $property"
}
proc get_iobanks {args} {return [lindex $args end]}
proc get_slrs {args} {return [expr {[lindex $args end] eq "19" ? "SLR0" : "SLR1"}]}
proc get_cells {args} {
  set of [lsearch -exact $args -of_objects]
  if {$of>=0} {return [lmap pin [lindex $args [expr {$of+1}]] {file dirname $pin}]}
  set filter [lindex $args end]
  if {[string first native_bank19_rx $filter]>=0} {return {native_bank19_rx/local native_bank19_rx/phy native_bank19_rx/fixed}}
  if {[string first native_bank29_rx $filter]>=0} {return {native_bank29_rx/local}}
  if {[string first __emuflow_transport__ $filter]>=0} {return [list $::shared $::shadow $::counter]}
  error "unexpected cell request"
}
proc get_pins {args} {return [list $::counter/Q]}
proc get_nets {args} {return control_net}
proc set_property {property value objects} {
  if {$property eq "MAX_FANOUT_MODE"} {
    if {$value ne "CLOCK_REGION" || $objects ne "control_net"} {error "wrong control replication"}
    set ::replication 1;return
  }
  if {$property ne "IS_SOFT" || $value ne "false"} {error "unexpected preservation or timing property"}
}
set groups [dict create]
proc create_pblock {name} {return $name}
proc add_cells_to_pblock {name objects} {dict set ::groups $name [lsort $objects]}
proc resize_pblock {name args} {
  if {$args ne [list -add [string range $name end-3 end] -locs trim]} {error "wrong native SLR region"}
}
foreach command {create_clock set_max_delay set_false_path set_case_analysis unplace_cell read_checkpoint} {
  proc $command {args} {error "placement added an unrelated operation"}
}
set sources [dict create rx [dict create s1 [list $shared/C] s2 [list $shared/C] s3 [list $shadow/C]] infrastructure_clock_pins [dict create $shared [list $shared/C] $shadow [list $shadow/C] $counter [list $counter/C]]]
'''
    script += 'set result [emuflow_native_bank_placement ' + _tcl_word(placement) + ' $sources ' + _tcl_word(output) + ' mapped_partition]\n'
    script += r'''
if {[dict get $result ambiguous_native_cells]!=1 || [dict get $result missing_rx_roles]!=1} {error "optimized native roles were not explicit"}
if {[dict get $::groups emuflow_bank_affinity_SLR0] ne [lsort [list native_bank19_rx/local $::shadow]]} {error "wrong bank0 placement group"}
if {[dict get $::groups emuflow_bank_affinity_SLR1] ne {native_bank29_rx/local}} {error "wrong bank1 placement group"}
if {![info exists ::replication]} {error "control locality preference absent"}
puts complete
'''
    completed = subprocess.run([TCL86], input=script, text=True, capture_output=True)
    assert completed.returncode == 0 and not completed.stderr, completed.stderr
    assert completed.stdout.strip() == 'complete'
