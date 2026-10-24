"""Borrowers and exception cycles must unmap before retiring native scratch."""
import gc
import mmap
from pathlib import Path
import struct
from unittest import mock

import pytest
from emuflow.errors import ValidationError
from emuflow.native_columns import _ClusterScratch
from emuflow import phase3_exact_records as records


def artifact(kind):
    def string(value):
        raw=value.encode();return struct.pack('<I',len(raw))+raw
    if kind=='nodes':
        raw=b''.join(string(value) for value in ('net0','combinational','fpga0'))
        raw+=struct.pack('<6I',0,1,1,0,1,1)
        raw+=b''.join(string(value) for value in ('fpga1','segment000000','segment000001'))
        raw+=b'\x01'
        magic,version=0x314E4553464D5545,2
    else:
        fields=(b'net0',b'fpga1',b'ff0',b'D',b'\x00')
        raw=struct.pack('<B5I',1,*(len(value) for value in fields))+b''.join(fields)
        magic,version=0x31434553464D5545,1
    return struct.pack('<QIIQ2Q',magic,version,1,len(raw),0,len(raw))+raw


def read(path,kind):
    return (records.read_native_static_exact_cut_node_stream if kind=='nodes'
            else records.read_native_static_exact_capture_stream)(path)


def owned(kind):
    scratch=_ClusterScratch(prefix='emuflow-native-record-lifetime-')
    root=Path(scratch.name);path=root/'records.bin';path.write_bytes(artifact(kind))
    sequence=read(path,kind)
    sequence.adopt_native_scratch(scratch)
    return sequence,root,sequence._native_mapping[0]


@pytest.mark.parametrize('kind',['nodes','captures'])
@pytest.mark.parametrize('early_stop',[False,True])
def test_record_iterator_borrows_mapping_and_scratch_until_consumed(kind,early_stop):
    sequence,root,data=owned(kind)
    iterator=iter(sequence)
    if early_stop:next(iterator)
    del sequence;gc.collect()
    assert root.exists() and not data.closed
    if early_stop:iterator.close()
    else:assert len(list(iterator))==1
    gc.collect()
    assert data.closed and not root.exists()


@pytest.mark.parametrize('kind',['nodes','captures'])
def test_borrowed_native_scalar_callable_retains_mapping(kind):
    sequence,root,data=owned(kind)
    value=sequence.native_dependency_indices if kind=='nodes' else sequence._binary_indexer
    del sequence;gc.collect()
    assert root.exists() and not data.closed
    assert value(0)==() if kind=='nodes' else value(0)[0]==b'net0'
    del value;gc.collect()
    assert data.closed and not root.exists()


def test_binary_capture_iterator_borrows_owner_before_first_next():
    sequence,root,data=owned('captures')
    iterator=sequence.iter_binary_captures()
    del sequence;gc.collect()
    assert root.exists() and not data.closed
    assert next(iterator)[0]==b'net0'
    iterator.close();gc.collect()
    assert data.closed and not root.exists()


@pytest.mark.parametrize('kind',['nodes','captures'])
def test_traceback_cycle_has_one_ordered_unmap_and_retirement_owner(kind):
    errors=[]
    with mock.patch('sys.unraisablehook',side_effect=lambda event:errors.append(event.exc_value)):
        def fail():
            sequence,root,data=owned(kind)
            try:raise ValueError('consumer failed')
            except ValueError as error:
                cycle=[sequence,error];cycle.append(cycle)
                return root,data
        root,data=fail();gc.collect()
    assert data.closed and not root.exists()
    assert not errors


@pytest.mark.parametrize('kind',['nodes','captures'])
def test_explicit_close_invalidates_borrowed_cursor_and_is_idempotent(kind):
    sequence,root,data=owned(kind)
    iterator=iter(sequence)
    sequence.close();sequence.close()
    assert data.closed and not root.exists()
    with pytest.raises(ValueError,match='mapping is closed'):next(iterator)


@pytest.mark.parametrize('kind',['nodes','captures'])
@pytest.mark.parametrize('damage',['version','offset','length'])
def test_rejected_reader_closes_even_with_retained_mapping_and_traceback(tmp_path,kind,damage):
    path=tmp_path/'published.bin';raw=bytearray(artifact(kind))
    if damage=='version':struct.pack_into('<I',raw,8,99)
    elif damage=='offset':struct.pack_into('<Q',raw,24,1)
    else:raw=raw[:-1]
    path.write_bytes(raw)
    factory=mmap.mmap;held=[]
    def retain(*args,**kwargs):
        data=factory(*args,**kwargs);held.append(data);return data
    with mock.patch.object(records.mmap,'mmap',side_effect=retain):
        with pytest.raises(ValidationError) as error:read(path,kind)
    assert error.value and held and all(data.closed for data in held)
    assert path.read_bytes()==raw


@pytest.mark.parametrize('kind',['nodes','captures'])
def test_external_published_input_is_not_retired_with_its_reader(tmp_path,kind):
    path=tmp_path/'published.bin';path.write_bytes(artifact(kind))
    sequence=read(path,kind);data=sequence._native_mapping[0]
    sequence.close()
    assert data.closed and path.read_bytes()==artifact(kind)


@pytest.mark.parametrize('kind',['nodes','captures'])
def test_retirement_callback_runs_only_after_unmapping(kind):
    sequence,root,data=owned(kind)
    cleanup=_ClusterScratch.cleanup
    observed=[]
    def strict(scratch):
        assert data.closed
        observed.append(scratch.name)
        cleanup(scratch)
    with mock.patch.object(_ClusterScratch,'cleanup',new=strict):
        sequence.close()
    assert observed==[str(root)] and not root.exists()
