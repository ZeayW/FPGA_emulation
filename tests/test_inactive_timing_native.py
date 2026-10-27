"""Inactive evidence uses the same source-bound native producer as routed data."""
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.physical_measurements_native import publish_empty_partition_timing
from tests.logic_identity_fixture import identity_facts
from tests.timing_facts import local_timing_facts


def _identity(root, kind, *, nonempty=False, unsupported=False):
    if kind==2:
        from tests.test_boundary_identity_storage import _header, _record
        from emuflow.boundary_identity_storage import write_boundary_identity_storage
        path=root/'identity.json'
        with write_boundary_identity_storage(path,_header(),[_record('tx','tx',0)] if nonempty else []):pass
        return path
    if kind==3:
        from tests.test_logic_segment_timing import LogicSegmentTimingTest
        path,header=LogicSegmentTimingTest._typed_identity(root, int(nonempty))
        if unsupported:
            with identity_facts(path,header=header,segments=[],
                    unsupported_member_paths=[{'path':'unsupported-member','reason':'source-unsupported'}]):pass
        return path
    from tests.test_local_path_timing_storage import _header,_record
    from emuflow.local_path_timing import LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION
    path=root/'identity.json';header=_header()
    header.update(qualification=LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION)
    header['coverage']={'local_paths':int(nonempty)}
    with local_timing_facts(path,header,[_record('a',0.0)] if nonempty else []):pass
    return path


@pytest.mark.parametrize('kind',[2,3,4])
def test_inactive_stage_publishes_empty_current_evidence_without_generic_writer(tmp_path,kind):
    import emuflow.logic_segment_timing_storage as logic
    import emuflow.local_path_timing_storage as local
    from emuflow import boundary_timing_storage as boundary
    assert not hasattr(boundary,'write_boundary_timing_storage')
    assert not hasattr(logic,'write_logic_segment_timing_storage')
    assert not hasattr(local,'write_local_path_timing_storage')
    path=_identity(tmp_path,kind,unsupported=kind==3)
    from emuflow.multi_fpga_physical_flow import _write_inactive_logic_timing,_write_inactive_local_timing
    result = publish_empty_partition_timing(path,tmp_path/'timing.json',kind=2,
        provider='inactive-empty-partition-v1') if kind==2 else (
        _write_inactive_logic_timing if kind==3 else _write_inactive_local_timing)(path,tmp_path/'timing.json')
    assert result['maximum_delay_ns']==0.0
    reader={2:boundary.read_boundary_timing_storage,3:logic.read_logic_segment_timing_storage,4:local.read_local_path_timing_storage}[kind]
    with reader(tmp_path/'timing.json') as view:
        assert view['provider']=='inactive-empty-partition-v1'
        assert view['qualification']=='not-applicable-empty-partition'
        assert len(view[{2:'endpoints',3:'segments',4:'paths'}[kind]])==0
        if kind==3:
            assert list(view['unsupported_member_paths'])==[{'path':'unsupported-member','reason':'source-unsupported'}]


@pytest.mark.parametrize('kind',[2,3,4])
def test_inactive_stage_rejects_nonempty_identity_without_replacing_evidence(tmp_path,kind):
    path=_identity(tmp_path,kind)
    output=tmp_path/'timing.json'
    publish_empty_partition_timing(path,output,kind=kind,provider='prior')
    old=output.read_bytes()
    nonempty=tmp_path/'nonempty';nonempty.mkdir()
    current=_identity(nonempty,kind,nonempty=True)
    with pytest.raises(ValidationError,match='empty identity population'):
        publish_empty_partition_timing(current,output,kind=kind,provider='changed')
    assert output.read_bytes()==old


@pytest.mark.parametrize('kind',[2,3,4])
def test_inactive_source_change_before_selector_commit_preserves_prior_generation(tmp_path,kind):
    from emuflow import physical_measurements_native as native
    path=_identity(tmp_path,kind)
    output=tmp_path/'timing.json'
    publish_empty_partition_timing(path,output,kind=kind,provider='prior')
    old=output.read_bytes();selector=read_json(output)
    payload=output.parent/selector['artifact'];old_payload=payload.read_bytes()
    source=path.parent/read_json(path)['artifact']
    original=native.commit_store_selector
    def changed(*args,**kwargs):
        source.chmod(source.stat().st_mode ^ 0o100)
        return original(*args,**kwargs)
    with patch.object(native,'commit_store_selector',changed):
        with pytest.raises(ValidationError,match='generation changed'):
            publish_empty_partition_timing(path,output,kind=kind,provider='changed')
    assert output.read_bytes()==old and payload.read_bytes()==old_payload
    assert list(tmp_path.glob('timing.json.store.*.sqlite'))==[payload]
    assert not list(tmp_path.glob('.inactive-timing.*'))
