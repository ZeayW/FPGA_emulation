"""Complete exact-file transaction; independent fixture oracle stays in tests."""
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.phase3_exact_binary import validate_phase3_exact_binary, write_phase3_exact_binary
from tests.phase3_exact_reference import validate_reference
from tests.test_phase3_exact_binary import _contract
from tests.test_phase3_semantics_native import selected, build


@pytest.mark.parametrize('version', [1, 2])
def test_final_native_file_matches_reference_bytes_and_independent_validation(selected,tmp_path,version):
    actual=build(selected);contract=actual['semantic_contract'];parts=sorted(fpga.id for fpga in selected[1].fpgas)
    plain={key:list(value) if key in {'cut_nodes','dependency_edges','logic_segments','capture_requirements'} else value
        for key,value in contract.items()}
    expected=tmp_path/'reference.bin';output=tmp_path/'native.bin'
    expected_descriptor=write_phase3_exact_binary(expected,plain,parts,_format_version=version)
    with (patch('emuflow.phase3_exact_binary._Spool',side_effect=AssertionError('host section spool')),
          patch('emuflow.phase3_exact_binary._read_nets',side_effect=AssertionError('host net catalog')),
          patch('emuflow.phase3_exact_binary._SectionReader',side_effect=AssertionError('host binary records'))):
        descriptor=write_phase3_exact_binary(output,contract,parts,_format_version=version)
        proof=validate_phase3_exact_binary(output,descriptor)
    assert descriptor==expected_descriptor
    assert output.read_bytes()==expected.read_bytes()
    assert proof==validate_reference(output,descriptor)
    assert not list(tmp_path.glob('.native-exact-file-*'))


def test_native_failure_preserves_previous_destination_and_source(selected,tmp_path):
    actual=build(selected);contract=actual['semantic_contract'];source=contract['cut_nodes'].direct_binary_source
    paths=source.native_record_paths();before=[Path(path).read_bytes() for path in paths]
    output=tmp_path/'preserved.bin';output.write_bytes(b'previous-generation')
    changed=dict(contract);changed['metrics']={**contract['metrics'],'logic_segments':contract['metrics']['logic_segments']+1}
    with pytest.raises(ValidationError,match='metrics'):
        write_phase3_exact_binary(output,changed,sorted(fpga.id for fpga in selected[1].fpgas),semantic_digest=actual['semantic_contract_sha256'])
    assert output.read_bytes()==b'previous-generation'
    assert [Path(path).read_bytes() for path in paths]==before
    assert not list(tmp_path.glob('.native-exact-file-*'))


def test_ordinary_reference_file_validator_has_no_host_graph_path(tmp_path,monkeypatch):
    from tests.native_build import emuflow_native
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    path=tmp_path/'fixture.bin';descriptor=write_phase3_exact_binary(path,_contract(),['fpga0','fpga1'])
    expected=validate_reference(path,descriptor)
    with (patch('emuflow.phase3_exact_binary._load_manifest',side_effect=AssertionError('host manifest')),
          patch('emuflow.phase3_exact_binary._read_nets',side_effect=AssertionError('host names')),
          patch('emuflow.phase3_exact_binary.iter_phase3_exact_node_sources',side_effect=AssertionError('host sources'))):
        assert validate_phase3_exact_binary(path,descriptor)==expected
