"""Primary cluster construction in the common native core.

Only packed eligibility/group identities cross the request boundary. The native
owner performs connectivity union, canonical membership and resource reduction;
Python consumers borrow read-only columns rather than rebuilding those arrays.
"""
from __future__ import annotations
from array import array
import mmap
import json
import os
from pathlib import Path
import struct
import shutil
import sys
import tempfile
import zlib

from .errors import ValidationError
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import NativeToolProcess


from .native_columns import _ClusterScratch, _ColumnOwner, _MappedColumn, _MappedNames


def _load_columns(output, scratch, expected_cells, fixed, groups):
    from .partition import _CompactClusterRecords
    with output.open('rb') as stream:
        data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    owner = _ColumnOwner(data,scratch)
    try:
        if len(data) < 40:
            raise ValidationError('native cluster columns are truncated')
        magic, version, cells, clusters, names = struct.unpack_from('<QIQQQ', data)
        expected = 36 + 8*(cells+1) + 4*(clusters+1) + 8*cells + 48*clusters + names + 4
        if (magic != 0x314C4350464D5545 or version != 1 or cells != expected_cells
                or clusters > cells or len(data) != expected):
            raise ValidationError('native cluster columns have invalid dimensions')
        crc = 0
        for offset in range(0, len(data)-4, 1024*1024):
            crc = zlib.crc32(data[offset:min(offset+1024*1024,len(data)-4)], crc)
        if crc != struct.unpack_from('<I', data, len(data)-4)[0]:
            raise ValidationError('native cluster columns checksum disagrees')
        offset = 36
        def column(code, count):
            nonlocal offset
            width = struct.calcsize(code)
            result = _MappedColumn(owner,offset,count,code)
            offset += width*count
            return result
        name_offsets = column('Q', cells+1)
        member_offsets = column('I', clusters+1)
        members = column('I', cells)
        owners = column('I', cells)
        resources = tuple(column('I', clusters) for _ in range(12))
        name_bytes = _MappedNames(owner,offset,names)
        if name_offsets[0] != 0 or name_offsets[-1] != names:
            raise ValidationError('native cluster name bounds disagree')
        fixed_by_cluster = {}
        for source, fpga in fixed:
            cluster = owners[source]
            previous = fixed_by_cluster.setdefault(cluster, fpga)
            if previous != fpga:
                raise ValidationError('native cluster has conflicting fixed FPGA constraints')
        groups_by_cluster = {}
        for identity, sources in groups:
            for source in sources:
                groups_by_cluster.setdefault(owners[source], set()).add(identity)
        result = _CompactClusterRecords(name_bytes=name_bytes, name_offsets=name_offsets,
            member_offsets=member_offsets, member_ordinals=members, resource_columns=resources,
            source_cluster_indices=owners, fixed_by_cluster=fixed_by_cluster,
            groups_by_cluster={key:sorted(value) for key,value in groups_by_cluster.items()})
        result.implementation_id = 'native-primary-clusters-v1'
        return result
    except BaseException:
        owner.close()
        raise


def build_native_clusters(ir, constraints, eligible):
    from .emuir_storage import open_emuir_sqlite_read_only
    if sys.byteorder != 'little':
        raise ValidationError('native primary clusters require little-endian storage')
    cells, nets = len(ir.value['instances']), len(ir.value['nets'])
    scratch = _ClusterScratch()
    root = Path(scratch.name)
    try:
        source = Path(ir.path)
        generation = native_file_generation(source)
        connection = open_emuir_sqlite_read_only(source)
        try:
            def source_seq(identity):
                row = connection.execute('SELECT seq FROM cell_records WHERE id=?',(identity,)).fetchone()
                if row is None or not 0 <= row[0] < cells:
                    raise ValidationError('native cluster constraint references unknown cell')
                return row[0]
            groups = [(group['id'], array('I',(source_seq(name) for name in group['instances'])))
                      for group in constraints['groups']]
            fixed = [(source_seq(item['instance']),item['fpga']) for item in constraints['fixed']]
            bits = getattr(eligible, '_ordinal_bits', None)
            if bits is None:
                bits = bytearray((nets+7)//8)
                if eligible:
                    for row in connection.execute('SELECT seq,id FROM net_records ORDER BY seq'):
                        if row['id'] in eligible:
                            bits[row['seq']>>3] |= 1 << (row['seq']&7)
        finally:
            connection.close()
        request, output = root/'request.bin', root/'clusters.bin'
        with request.open('wb') as stream:
            stream.write(struct.pack('<QIQQQqqqqQQQ',0x31515243464D5545,1,*generation,cells,nets,len(bits)))
            chunks = getattr(bits, 'iter_bytes', None)
            if chunks is None:
                stream.write(bits)
            else:
                for block in chunks():
                    stream.write(block)
            stream.write(struct.pack('<Q',len(groups)))
            for _identity, members in groups:
                stream.write(struct.pack('<Q',len(members)))
                stream.write(memoryview(members).cast('B'))
        executable = resolve_native_executable('emuflow-native')
        report = root/'operation.json'
        command = [executable,'--report',str(report),'phase3','build-clusters',str(source),str(request),str(output)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),timeout_ms=21600000,
                               scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native primary cluster construction failed: '+completed.stdout)
        if native_file_generation(source) != generation:
            raise ValidationError('native primary cluster source generation changed')
        if report.stat().st_size > 65536:
            raise ValidationError('native primary cluster metrics exceed bound')
        operation=json.loads(report.read_text())
        if (not isinstance(operation,dict) or operation.get('schema')!='emuflow.native-stage-result/v1'
                or operation.get('stage')!='phase3.build-clusters' or operation.get('status')!='pass'
                or not isinstance(operation.get('metrics'),dict)):
            raise ValidationError('native primary cluster operation report disagrees')
        request.unlink()
        report.unlink()
        result = _load_columns(output,scratch,cells,fixed,groups)
        result.native_metrics = operation['metrics']
        return result
    except BaseException:
        scratch.cleanup()
        raise
