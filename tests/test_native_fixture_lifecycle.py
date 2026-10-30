"""Fixture owners close mappings before removing their shared source files."""
import gc
from unittest.mock import patch
import weakref

import pytest

from tests.native_column_fixture import _ColumnOwner
from tests.native_partition_facts import _Leases


@pytest.mark.parametrize("explicit_close", [False,True])
def test_mapping_lease_outlives_close_even_inside_a_collection_cycle(explicit_close):
    events=[]

    class Mapping:
        closed=False
        def close(self):
            if not self.closed:
                events.append("close")
                self.closed=True

    mapping=Mapping()

    class Scratch:
        name="lifecycle-fixture"
        def cleanup(self):
            events.append("cleanup")
            assert mapping.closed, "source files removed while mapping is live"

    with patch("tests.native_partition_facts._ClusterScratch",return_value=Scratch()):
        group=_Leases()
    lease=group.lease()
    owner=_ColumnOwner(mapping,lease)
    owner.cycle=lease
    lease.cycle=owner
    weak_owner=weakref.ref(owner)
    group.release()
    assert events==[]
    if explicit_close:
        owner.close()
        owner.close()
    del owner,lease
    gc.collect()
    assert weak_owner() is None
    assert events==["close","cleanup"] and group.count==0


def test_column_snapshot_keeps_no_original_descriptor_for_a_forked_reader(tmp_path):
    import os
    from pathlib import Path
    import struct
    from tests.native_column_fixture import _ClusterScratch, _MappedColumn, read_fixture_columns
    if not hasattr(os,"fork"):
        pytest.skip("descriptor inheritance test requires fork")
    scratch=_ClusterScratch(prefix="fixture-source-lifetime-")
    source=Path(scratch.name)/"columns.bin"
    source.write_bytes(struct.pack("<3I",3,5,8))
    owner=_ColumnOwner(read_fixture_columns(source),scratch)
    column=_MappedColumn(owner,0,3,"I")
    assert column.view.readonly and list(column)==[3,5,8]
    if Path("/proc/self/fd").is_dir():
        for fd in Path("/proc/self/fd").iterdir():
            try:
                target=os.readlink(fd)
            except OSError:
                continue
            assert str(source) not in target
    ready_read,ready_write=os.pipe()
    exit_read,exit_write=os.pipe()
    child=os.fork()
    if child==0:
        try:
            os.close(ready_read);os.close(exit_write)
            os.write(ready_write,b"1" if list(column)==[3,5,8] else b"0")
            os.read(exit_read,1)
            os._exit(0 if list(column)==[3,5,8] else 1)
        except BaseException:
            os._exit(2)
    os.close(ready_write);os.close(exit_read)
    try:
        assert os.read(ready_read,1)==b"1"
        owner.close()
        assert owner.data.closed and not Path(scratch.name).exists()
    finally:
        os.write(exit_write,b"1")
        os.close(exit_write);os.close(ready_read)
        _,status=os.waitpid(child,0)
    assert os.waitstatus_to_exitcode(status)==0


def test_column_snapshot_rejects_a_changed_original_generation(tmp_path):
    from pathlib import Path
    from emuflow.common.errors import ValidationError
    from emuflow.common.native_tools import native_file_generation
    from tests.native_column_fixture import read_fixture_columns
    source=tmp_path/"columns.bin";source.write_bytes(b"original")
    before=native_file_generation(source)
    with patch("emuflow.common.native_tools.native_file_generation",side_effect=[before,(*before[:2],before[2]+1,*before[3:])]):
        with pytest.raises(ValidationError,match="source generation changed"):
            read_fixture_columns(source)
