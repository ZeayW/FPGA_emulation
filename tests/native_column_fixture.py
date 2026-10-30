"""Test-only borrowed columns for bounded native protocol fixtures."""
from pathlib import Path
import mmap
import shutil
import struct
import tempfile


def read_fixture_columns(path):
    """Snapshot bounded fixture bytes without retaining a source descriptor.

    File-backed mmap descriptors can be inherited by a concurrently forked tool
    while cyclic fixture finalizers remove their NFS source. Anonymous mappings
    preserve the column buffer/close contract; original source generations are
    still independently bound and checked by the native protocol drivers.
    """
    from emuflow.common.errors import ValidationError
    from emuflow.common.native_tools import native_file_generation
    path = Path(path)
    before = native_file_generation(path)
    size = before[2]
    if not 0 < size <= 128 * 1024 * 1024:
        raise ValidationError("native column fixture exceeds its byte bound")
    data = mmap.mmap(-1, size)
    try:
        with path.open("rb") as stream:
            offset = 0
            while offset < size:
                chunk = stream.read(min(1024 * 1024, size - offset))
                if not chunk:
                    raise ValidationError("native column fixture is truncated")
                data[offset:offset + len(chunk)] = chunk
                offset += len(chunk)
            if stream.read(1):
                raise ValidationError("native column fixture grew during snapshot")
        if native_file_generation(path) != before:
            raise ValidationError("native column fixture source generation changed")
        return data
    except BaseException:
        data.close()
        raise


class _ClusterScratch:
    # A separate TemporaryDirectory weakref finalizer can run before the
    # mapping owner's __del__ in a traceback cycle. On NFS that unlinks the
    # still-open file and races .nfs removal. The mapping owner alone orders
    # view release, unmap and directory removal; setup failures clean explicitly.
    def __init__(self, prefix='phase3-primary-clusters-'):
        self.name = tempfile.mkdtemp(prefix=prefix)

    def cleanup(self):
        if Path(self.name).exists():
            shutil.rmtree(self.name)


class _ColumnOwner:
    """Own the mapping, views and scratch; borrowers retain this owner.

    Views are never exported. Explicit release before unmapping also works when
    an exception traceback forms a garbage-collection cycle around consumers.
    """
    def __init__(self, data, scratch=None):
        self.data, self.scratch, self.views = data, scratch, []

    def close(self):
        for view in self.views:
            view.release()
        self.views.clear()
        self.data.close()
        if self.scratch is not None:
            self.scratch.cleanup()

    def __del__(self):
        self.close()


class _MappedColumn:
    readonly = True

    def __init__(self, owner, offset, count, code):
        self.owner, self.offset = owner, offset
        self.itemsize = struct.calcsize(code)
        self.size = self.itemsize*count
        self.view = memoryview(owner.data)[offset:offset+self.size].cast(code).toreadonly()
        owner.views.append(self.view)

    def __len__(self):
        return len(self.view)

    def __iter__(self):
        yield from self.view

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self.view[index])
        return self.view[index]

    def __bytes__(self):
        return self.owner.data[self.offset:self.offset+self.size]

    def iter_bytes(self):
        for start in range(self.offset,self.offset+self.size,1024*1024):
            yield self.owner.data[start:min(start+1024*1024,self.offset+self.size)]


class _MappedNames:
    def __init__(self, owner, offset, size):
        self.owner, self.offset, self.size = owner, offset, size

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        begin,end,step=index.indices(self.size)
        if step != 1:
            return self.owner.data[self.offset:self.offset+self.size][index]
        return self.owner.data[self.offset+begin:self.offset+end]

    def __bytes__(self):
        return self.owner.data[self.offset:self.offset+self.size]

    def iter_bytes(self):
        for start in range(self.offset,self.offset+self.size,1024*1024):
            yield self.owner.data[start:min(start+1024*1024,self.offset+self.size)]

