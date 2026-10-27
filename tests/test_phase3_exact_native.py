"""Complete exact-file transaction; independent fixture oracle stays in tests."""
from tests.phase3_exact_fixture import write_exact_records_fixture, publish_exact_native_fixture
from pathlib import Path
import hashlib
import json
import struct
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.partition.phase3_exact_binary import (validate_phase3_exact_binary)
from tests.phase3_exact_reference import validate_reference
from tests.test_phase3_exact_binary import _contract
from tests.test_phase3_semantics_native import selected, build


def test_python_exact_publishers_and_section_spools_are_retired():
    from emuflow.partition import phase3_exact_binary; from emuflow.partition import phase3_exact_native
    assert not hasattr(phase3_exact_binary, 'write_phase3_exact_binary')
    assert not hasattr(phase3_exact_binary, '_Spool')
    assert not hasattr(phase3_exact_native, 'publish')


@pytest.mark.parametrize('version', [1, 2])
def test_final_native_file_matches_reference_bytes_and_independent_validation(selected,tmp_path,version):
    actual=build(selected);contract=actual['semantic_contract'];parts=sorted(fpga.id for fpga in selected[1].fpgas)
    plain={key:list(value) if key in {'cut_nodes','dependency_edges','logic_segments','capture_requirements'} else value
        for key,value in contract.items()}
    expected=tmp_path/'reference.bin';output=tmp_path/'native.bin'
    expected_descriptor=write_exact_records_fixture(expected,plain,parts,_format_version=version)
    with patch('emuflow.partition.phase3_exact_binary._read_nets', side_effect=AssertionError('host net catalog')), patch('emuflow.partition.phase3_exact_binary._SectionReader', side_effect=AssertionError('host binary records')):
        descriptor=publish_exact_native_fixture(output,contract,parts,_format_version=version)
        proof=validate_phase3_exact_binary(output,descriptor)
    assert descriptor==expected_descriptor
    assert output.read_bytes()==expected.read_bytes()
    assert proof==validate_reference(output,descriptor)
    assert not list(tmp_path.glob('native-exact-fixture-*'))


def test_native_failure_preserves_previous_destination_and_source(selected,tmp_path):
    actual=build(selected);contract=actual['semantic_contract'];source=contract['cut_nodes'].direct_binary_source
    paths=source.native_record_paths();before=[Path(path).read_bytes() for path in paths]
    output=tmp_path/'preserved.bin';output.write_bytes(b'previous-generation')
    changed=dict(contract);changed['metrics']={**contract['metrics'],'logic_segments':contract['metrics']['logic_segments']+1}
    with pytest.raises(ValidationError,match='metrics'):
        publish_exact_native_fixture(output,changed,sorted(fpga.id for fpga in selected[1].fpgas),semantic_digest=actual['semantic_contract_sha256'])
    assert output.read_bytes()==b'previous-generation'
    assert [Path(path).read_bytes() for path in paths]==before
    assert not list(tmp_path.glob('native-exact-fixture-*'))


def test_ordinary_reference_file_validator_has_no_host_graph_path(tmp_path,monkeypatch):
    from tests.native_build import emuflow_native
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    path=tmp_path/'fixture.bin';descriptor=write_exact_records_fixture(path,_contract(),['fpga0','fpga1'])
    expected=validate_reference(path,descriptor)
    with (patch('emuflow.partition.phase3_exact_binary._load_manifest',side_effect=AssertionError('host manifest')),
          patch('emuflow.partition.phase3_exact_binary._read_nets',side_effect=AssertionError('host names')),
          patch('emuflow.partition.phase3_exact_binary.iter_phase3_exact_node_sources',side_effect=AssertionError('host sources'))):
        assert validate_phase3_exact_binary(path,descriptor)==expected


@pytest.mark.parametrize('field',['schema','parts','sections','counts'])
def test_resealed_nul_metadata_key_cannot_alias_a_required_member(tmp_path,monkeypatch,field):
    from tests.native_build import emuflow_native
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    path=tmp_path/'original.bin';descriptor=write_exact_records_fixture(path,_contract(),['fpga0','fpga1'])
    assert validate_phase3_exact_binary(path,descriptor)['status']=='pass'
    data=path.read_bytes();magic,version,offset,length=struct.unpack_from('<8sIQQ',data)
    manifest=json.loads(data[offset:offset+length]);manifest[field+'\0other']=manifest.pop(field)
    encoded=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    damaged=tmp_path/'aliased.bin';damaged.write_bytes(struct.pack('<8sIQQ',magic,version,offset,len(encoded))+data[28:offset]+encoded)
    descriptor={**descriptor,'bytes':damaged.stat().st_size,'manifest_sha256':hashlib.sha256(encoded).hexdigest()}
    with pytest.raises(ValidationError):validate_reference(damaged,descriptor)
    with pytest.raises(ValidationError,match='Static Exact'):validate_phase3_exact_binary(damaged,descriptor)
