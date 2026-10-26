"""Device check import must keep all primitive/polarity/corner evidence."""
import json

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.xilinx_preplacement_timing import (import_vivado_ff_control_timing,
    _FF_CONTROLS, _FF_DELAY_FIELDS)


def raw_fixture(path):
    lines = ['schema\temuflow.vivado-ff-control-raw/v2', 'part\txcvu19p-fsva3824-2-e',
             'vivado_version\t2024.2', 'unit\tns', 'transition_frame\tlibrary-pin',
             '\t'.join(['cell_type','clock_inverted','reset_inverted','pin','kind','transitions',*_FF_DELAY_FIELDS])]
    for cell, pair in _FF_CONTROLS.items():
        for clock in ['0','1']:
            for reset in ['0','1']:
                for pin, kind in [('CE','setup'), pair]:
                    transitions = 'rr,rf' if kind == 'setup' else 'rf'
                    # A different corner/mode is worst for CE and reset.
                    values = ['-0.05']*12
                    values[0] = '.12'
                    values[3] = '.7' if clock == '1' and reset == '1' and pin=='CE' else '.2'
                    values[7] = '.9' if clock == '0' and reset == '1' and pin!='CE' else '.3'
                    if kind == 'recovery':
                        for i,field in enumerate(_FF_DELAY_FIELDS):
                            needed = 'f'
                            if ('r'if field.endswith('_RISE')else'f') != needed:values[i]=''
                        values[7] = '.9'if clock=='0'and reset=='1'else'.3'
                    lines.append('\t'.join([cell,clock,reset,pin,kind,transitions,*values]))
    path.write_text('\n'.join(lines)+'\n')
    return lines


def test_import_envelopes_every_mode_and_corner_without_using_data_setup(tmp_path):
    raw=tmp_path/'checks.tsv'; raw_fixture(raw)
    output=tmp_path/'timing.json'
    value=import_vivado_ff_control_timing(raw,output)
    assert value == json.loads(output.read_text())
    assert value['raw_sha256'] == file_sha256(raw)
    assert len(value['recipe_sha256']) == 64
    for cell,pins in value['checks'].items():
        assert pins['CE'] == dict(kind='setup',margin_ps=700.)
        pin,kind=_FF_CONTROLS[cell]
        assert pins[pin] == dict(kind=kind,margin_ps=900.)


@pytest.mark.parametrize('damage',['missing','duplicate','transition','corner','nonfinite','spurious','unit','version','truncated','oversized'])
def test_invalid_raw_checks_preserve_existing_output(tmp_path,damage):
    raw=tmp_path/'checks.tsv'; lines=raw_fixture(raw)
    if damage=='missing':lines.pop()
    elif damage=='duplicate':lines[-1]=lines[-2]
    elif damage=='transition':lines[-1]=lines[-1].replace('\trf\t','\trr\t')
    elif damage=='corner':lines[-1]=lines[-1].rsplit('\t',1)[0]
    elif damage in {'nonfinite','spurious'}:
        row=lines[-1].split('\t');row[7 if damage=='nonfinite'else 6]='nan'if damage=='nonfinite'else'.3'
        lines[-1]='\t'.join(row)
    elif damage=='unit':lines[3]='unit\tps'
    elif damage=='version':lines[2]='vivado_version\t'
    text='\n'.join(lines)+(''if damage=='truncated'else'\n')
    if damage=='oversized':text+='x'*65536
    raw.write_text(text)
    output=tmp_path/'timing.json';output.write_text('existing')
    with pytest.raises(ValidationError):import_vivado_ff_control_timing(raw,output)
    assert output.read_text()=='existing'


def test_import_cannot_replace_its_raw_source(tmp_path):
    raw=tmp_path/'checks.tsv';raw_fixture(raw);before=raw.read_bytes()
    with pytest.raises(ValidationError,match='aliases'):import_vivado_ff_control_timing(raw,raw)
    assert raw.read_bytes()==before
