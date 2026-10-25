"""Validate literal IP configuration handling without substituting for Vivado."""
from pathlib import Path
import shutil
import subprocess

import pytest

from emuflow.vivado_session import _tcl_word


TCL = shutil.which('tclsh')
ROOT = Path(__file__).resolve().parents[1]
TCL_VERSION = subprocess.run([TCL], input='puts [info patchlevel]\n', capture_output=True, text=True).stdout.strip() if TCL else '0'
HAS_BINARY_ENCODE = tuple(int(x) for x in TCL_VERSION.split('.')[:2]) >= (8, 6)
pytestmark = pytest.mark.skipif(TCL is None, reason='Tcl interpreter required')


def inspect(tmp_path, configuration, *, ignore=False):
    config = tmp_path/'config.tsv'
    config.write_text(configuration)
    output = tmp_path/'observations'
    script = tmp_path/'driver.tcl'
    source = r'''
proc get_parts {args} {return xcpart}
proc create_project {args} {}
proc get_ipdefs {args} {return xilinx.com:ip:high_speed_selectio_wiz:3.6}
proc create_ip {args} {}
proc get_ips {args} {return probe}
proc list_property {ip} {return {CONFIG.BUS_DIR CONFIG.SERIALIZATION_FACTOR CONFIG.ENABLE_BYTE0_PIN0}}
proc set_property {property value ip} {dict set ::properties $property $value}
proc get_property {property ip} {if {[dict exists $::properties $property]} {return [dict get $::properties $property]} {return false}}
proc version {args} {return 2024.2}
proc generate_target {target ip} {set ::generated 1}
proc report_ip_status {flag path} {set stream [open $path w];puts $stream generated;close $stream}
proc close_project {} {if {![info exists ::generated]} {error "output generation missing"}}
'''
    if ignore:
        source += '\nrename set_property accepted_set_property\nproc set_property {property value ip} {dict set ::properties $property [expr {$property eq "CONFIG.BUS_DIR" ? 1 : $value}]}\n'
    arguments = ['xcpart', str(output), str(config)]
    source += '\nset argv [list '+' '.join(_tcl_word(x) for x in arguments)+']\nset argc 3\n'
    source += 'source '+_tcl_word(ROOT/'scripts/vivado/inspect_selectio_ip.tcl')+'\n'
    script.write_text(source)
    return subprocess.run([TCL, str(script)], capture_output=True, text=True), output


@pytest.mark.skipif(not HAS_BINARY_ENCODE, reason="IP inspection uses Vivado Tcl 8.6 binary encoding")
def test_literal_configuration_generates_outputs_without_evaluating_values(tmp_path):
    marker = tmp_path/'must-not-exist'
    literal = '[open '+str(marker)+' w]; $unsafe'
    result, output = inspect(tmp_path, 'property\tvalue\nCONFIG.BUS_DIR\t'+literal+'\nCONFIG.SERIALIZATION_FACTOR\t8\n')
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    properties = {}
    for row in (output/'ip-properties.tsv').read_text().splitlines()[1:]:
        key, value = row.split('\t')
        properties[bytes.fromhex(key).decode()] = bytes.fromhex(value).decode()
    assert properties == {'CONFIG.BUS_DIR': literal, 'CONFIG.SERIALIZATION_FACTOR': '8', 'CONFIG.ENABLE_BYTE0_PIN0': 'false'}
    assert (output/'ip-status.rpt').read_text().strip() == 'generated'


@pytest.mark.parametrize('rows', [
    'property\tvalue\nCONFIG.BUS_DIR\t0\nCONFIG.BUS_DIR\t1\n',
    'property\tvalue\nCONFIG.UNKNOWN\t0\n',
    'property\tvalue\nCONFIG.ENABLE_BYTE0_PIN0\tTRUE\n',
    'property\tvalue\nCONFIG.ENABLE_BYTE0_PIN0\t1\n',
    'property\tvalue\nCONFIG.BUS_DIR\t0\textra\n',
    'bad\theader\nCONFIG.BUS_DIR\t0\n',
    'property\tvalue\n',
])
def test_malformed_or_unknown_ip_configuration_fails_before_generation(tmp_path, rows):
    result, output = inspect(tmp_path, rows)
    assert result.returncode != 0
    assert not (output/'ip-status.rpt').exists()


def test_ignored_ip_configuration_is_a_failure_even_when_the_vendor_command_returns(tmp_path):
    result, output = inspect(tmp_path, 'property\tvalue\nCONFIG.BUS_DIR\t0\nCONFIG.SERIALIZATION_FACTOR\t8\n', ignore=True)
    assert result.returncode != 0
    assert 'ignored or changed' in result.stderr
    assert not (output/'ip-status.rpt').exists()
