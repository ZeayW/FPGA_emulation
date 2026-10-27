"""The independent small-design oracle reads only bounded current typed facts."""
import json
from pathlib import Path
import sqlite3
import struct
from unittest.mock import patch

import pytest

from emuflow.partition.combinational_cut import semantic_contract_sha256
from emuflow.ir.emuir_compact import write_compact_emuir
from emuflow.common.errors import ValidationError
from emuflow.transport.phase6_oracle import read_bounded_assignment
from tests.phase3_store_fixture import write_current_assignment_fixture
import tests.test_combinational_cut as fixture_module


@pytest.fixture
def inputs(tmp_path):
    case=fixture_module.StaticExactCombinationalCutPartitionTest();case.setUp()
    _,assignment=case._exact_artifacts(dependent_return=True)
    source=tmp_path/'source.sqlite';write_compact_emuir(source,case.ir.value)
    path=tmp_path/'assignment.json'
    write_current_assignment_fixture(path,case.ir,assignment)
    return source,path,assignment


def test_current_oracle_recovers_every_ownership_and_exact_record(inputs):
    source,path,expected=inputs
    actual=read_bounded_assignment(source,path)
    assert actual['instance_assignment']==dict(expected['instance_assignment'])
    assert actual['cut_nets']==list(expected['cut_nets'])
    assert actual['semantic_contract']==expected['semantic_contract']
    assert semantic_contract_sha256(actual['semantic_contract'])==expected['semantic_contract_sha256']


@pytest.mark.parametrize('field,limit',[('cell_records',10000),('endpoint_records',100000)])
def test_oracle_checks_population_before_reading_assignment(inputs,field,limit):
    source,path,_=inputs
    with sqlite3.connect(source) as db:
        raw=db.execute("SELECT value FROM metadata WHERE key='record_counts_json'").fetchone()[0]
        counts=json.loads(raw);counts[field]=limit+1
        db.execute("UPDATE metadata SET value=? WHERE key='record_counts_json'",(json.dumps(counts),))
    with patch('emuflow.partition.phase3_storage.read_phase3_assignment_view',side_effect=AssertionError('unbounded read')):
        with pytest.raises(ValidationError,match='limited'):read_bounded_assignment(source,path)


def test_resealed_binary_header_cannot_change_the_oracle_canonical_contract(inputs):
    import hashlib
    source,path,_=inputs
    index=path.with_name('assignment.sqlite')
    with sqlite3.connect(index) as db:
        descriptor=json.loads(db.execute("SELECT value FROM metadata WHERE key='semantic_contract_binary'").fetchone()[0])
        binary=index.with_name(descriptor['artifact']);raw=binary.read_bytes()
        magic,version,offset,length=struct.unpack_from('<8sIQQ',raw)
        manifest=json.loads(raw[offset:offset+length]);manifest['contract_header']['oracle_tamper']='resealed'
        encoded=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
        binary.write_bytes(struct.pack('<8sIQQ',magic,version,offset,len(encoded))+raw[28:offset]+encoded)
        descriptor.update(bytes=binary.stat().st_size,manifest_sha256=hashlib.sha256(encoded).hexdigest())
        db.execute("UPDATE metadata SET value=? WHERE key='semantic_contract_binary'",(json.dumps(descriptor),))
    with pytest.raises(ValidationError,match='canonical identity disagrees'):read_bounded_assignment(source,path)


def test_oracle_rejects_replaced_typed_generation(inputs):
    import emuflow.partition.phase3_exact_binary as binary
    source,path,_=inputs;original=binary.iter_phase3_exact_segments
    def replace_after_read(*args,**kwargs):
        yield from original(*args,**kwargs)
        artifact=Path(args[0]);artifact.chmod(artifact.stat().st_mode^0o100)
    with patch.object(binary,'iter_phase3_exact_segments',side_effect=replace_after_read):
        with pytest.raises(ValidationError,match='generation changed'):read_bounded_assignment(source,path)
