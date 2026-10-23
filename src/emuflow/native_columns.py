"""Borrowed read-only column lifetime shared by thin native adapters."""
from pathlib import Path
import shutil
import struct
import tempfile


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
    def __init__(self, data, scratch):
        self.data, self.scratch, self.views = data, scratch, []

    def close(self):
        for view in self.views:
            view.release()
        self.views.clear()
        self.data.close()
        self.scratch.cleanup()

    def __del__(self):
        self.close()


class _MappedColumn:
    readonly = True

    def __init__(self, owner, offset, count, code):
        self.owner, self.offset = owner, offset
        self.itemsize = struct.calcsize(code)
        self.size = self.itemsize*count
        self.view = memoryview(owner.data)[offset:offset+self.size].cast(code)
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

