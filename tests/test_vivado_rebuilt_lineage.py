"""Source tags retain timing associations after hierarchy rebuilding."""
import csv
from pathlib import Path
import subprocess

import pytest

from emuflow.vivado_session import _tcl_word, prepare_timing
from tests.test_vivado_session import ROOT, TCL86, session_fixture


@pytest.mark.skipif(TCL86 is None, reason="Tcl 8.6 required")
def test_relocated_primitives_export_and_resolve_by_source_tags(tmp_path):
    session, _, _ = session_fixture(tmp_path, require_constant=False)
    output = Path(session['output'])
    inventory = output/'routed_mapped_cells.tsv'
    with inventory.open(newline='') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    program = f"source {_tcl_word(ROOT/'scripts/vivado/source_inventory.tcl')}\nset cells [dict create]\n"
    for index,row in enumerate(rows):
        program += f"dict set cells rebuilt_{index} [list {_tcl_word(row['ref_name'])} {_tcl_word(row['source_origin'])} {index}]\n"
    program += r'''
proc get_cells {args} {return [dict keys $::cells]}
proc get_property {args} {
 lassign [lrange $args end-1 end] key object
 if {$key eq "NAME"} {return $object}
 if {$key eq "COLUMN"} {regexp {([0-9]+)$} $object _ index;return $index}
 if {$key eq "ROW"} {return 0}
 lassign [dict get $::cells $object] ref origin index
 switch $key {
  REF_NAME {return $ref}
  EMUFLOW_SOURCE {return $origin}
  OPT_MODIFIED - PHYS_OPT_MODIFIED {return ""}
  PRIMITIVE_LEVEL {return LEAF}
  BEL {return [expr {[string match LUT* $ref] ? "A6LUT" : "AFF"}]}
 }
 error "unexpected property $key"
}
proc get_sites {args} {return SLICE_X[lindex [dict get $::cells [lindex $args end]] 2]Y0}
proc get_tiles {args} {regexp {X([0-9]+)Y0$} [lindex $args end] _ index;return tile_${index}}
'''
    program += f"puts [emuflow_inventory {_tcl_word(inventory)} mapped_partition 1]\n"
    done = subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)
    assert not done.stderr
    assert done.stdout.strip() == str(len(rows))
    preparation = prepare_timing(session)
    assert preparation['timing_coverage'] == {'status':'complete', 'requested_queries':1,
        'measurable_queries':1, 'unresolved_queries':0}
    with (output/'local_path_timing-resolved.tsv').open(newline='') as stream:
        resolved = list(csv.reader(stream, delimiter='\t'))[1]
    for candidates in resolved[-3:-1]:
        assert candidates
        assert all(bytes.fromhex(candidate).decode().startswith('rebuilt_') for candidate in candidates.split(','))
