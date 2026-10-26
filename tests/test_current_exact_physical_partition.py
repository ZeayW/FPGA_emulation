"""Current assignment generations must reach both physical query backends."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.combinational_cut import semantic_contract_sha256
from emuflow.logic_segment_timing import _stream_indexed_exact_partition
from emuflow.phase3_assignment_index import ASSIGNMENT_INDEX_APPLICATION_ID, CURRENT_DDL
from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT, write_phase3_exact_binary
from tests.test_phase3_exact_binary import _contract


@pytest.mark.parametrize('version',[3,4])
@pytest.mark.parametrize('fpga',['fpga0','fpga1'])
def test_supported_assignment_partition_streams_without_legacy_or_full_reconstruction(tmp_path,version,fpga):
    contract=_contract();digest=semantic_contract_sha256(contract)
    descriptor=write_phase3_exact_binary(tmp_path/EXACT_BINARY_ARTIFACT,contract,['fpga0','fpga1'],semantic_digest=digest)
    path=tmp_path/'assignment.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(CURRENT_DDL)
        db.execute(f'PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID}')
        db.execute(f'PRAGMA user_version={version}')
        for key,value in {'schema':f'emuflow.phase3-assignment-sqlite/v{version}',
                'semantic_contract_binary':descriptor,'semantic_contract_schema':contract['schema'],
                'semantic_contract_sha256':digest}.items():db.execute('INSERT INTO metadata VALUES(?,?)',(key,json.dumps(value)))
    with patch('emuflow.logic_segment_timing.read_phase3_exact_semantic_partition',side_effect=AssertionError('legacy/full reconstruction called')):
        header,segments,captures=_stream_indexed_exact_partition(path,fpga,allow_legacy=False)
        assert header=={'schema':contract['schema'],'sha256':digest,'fpga':fpga}
        assert list(segments)==[v for v in contract['logic_segments'] if v['fpga']==fpga]
        assert list(captures)==[v for v in contract['capture_requirements'] if v['fpga']==fpga]


def test_unknown_assignment_version_is_rejected_before_any_compatibility_attempt(tmp_path):
    path=tmp_path/'assignment.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(CURRENT_DDL)
        db.execute(f'PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID}')
        db.execute('PRAGMA user_version=99')
    with patch('emuflow.logic_segment_timing.read_phase3_exact_semantic_partition',side_effect=AssertionError('legacy called')):
        with pytest.raises(Exception,match='unsupported Phase 3 assignment index'):
            _stream_indexed_exact_partition(path,'fpga0',allow_legacy=True)
