"""Native candidate graph construction; Python carries only bounded policy."""
from __future__ import annotations
import json
import mmap
import os
from pathlib import Path
import struct
import sys
import zlib

from .combinational_cut import (_PackedEligibleCutIds, _STATE_RESOURCES,
    _HARD_COMBINATIONAL_RESOURCES, _TRANSPORT_SAFE_SEQUENTIAL_INPUTS)
from .errors import ValidationError
from .ir import VALID_CUT_CLASSES
from .native_columns import _ClusterScratch, _ColumnOwner, _MappedColumn, _MappedNames
from .native_tools import native_file_generation, resolve_native_executable
from .resources import RESOURCE_FIELDS
from .tool_runtime import NativeToolProcess


class _NativeEligibleCutIds(_PackedEligibleCutIds):
    __slots__ = ('implementation_id','native_metrics')


def write_candidate_policy(path, ir):
    source=Path(ir.path)
    generation=native_file_generation(source)
    with path.open('wb') as output:
        def put(code,*values):
            output.write(struct.pack('<'+code,*values))
        def text(value):
            encoded=value.encode('utf-8'); put('I',len(encoded)); output.write(encoded)
        def catalog(values):
            put('I',len(values))
            for value in values:
                text(value)
        put('QIQQQqqqqQQ',0x31514543464D5545,1,*generation,
            len(ir.value['instances']),len(ir.value['nets']))
        catalog(RESOURCE_FIELDS)
        mask=lambda fields:sum(1<<i for i,name in enumerate(RESOURCE_FIELDS) if name in fields)
        put('QQI',mask(_STATE_RESOURCES),mask(_HARD_COMBINATIONAL_RESOURCES),RESOURCE_FIELDS.index('lut'))
        text('$_DFF_'); catalog(['D'])
        put('I',len(_TRANSPORT_SAFE_SEQUENTIAL_INPUTS))
        for name, ports in sorted(_TRANSPORT_SAFE_SEQUENTIAL_INPUTS.items()):
            text(name); catalog(sorted(ports))
        catalog(sorted(VALID_CUT_CLASSES))
    return generation


def _read_ids(path, scratch, expected_nets):
    with path.open('rb') as stream:
        data=mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
    owner=_ColumnOwner(data,scratch)
    try:
        if len(data)<48:
            raise ValidationError('native candidate columns are truncated')
        magic,version,nets,eligible=struct.unpack_from('<QIQQ',data)
        names=struct.unpack_from('<Q',data,len(data)-12)[0]
        bits=(nets+7)//8
        if (magic!=0x31544343464D5545 or version!=1 or nets!=expected_nets or eligible>nets
                or len(data)!=28+bits+names+8*(eligible+1)+12):
            raise ValidationError('native candidate column dimensions disagree')
        crc=0
        for offset in range(0,len(data)-4,1024*1024):
            crc=zlib.crc32(data[offset:min(offset+1024*1024,len(data)-4)],crc)
        if crc!=struct.unpack_from('<I',data,len(data)-4)[0]:
            raise ValidationError('native candidate columns checksum disagrees')
        result=_NativeEligibleCutIds.__new__(_NativeEligibleCutIds)
        result._ordinal_bits=_MappedColumn(owner,28,bits,'B')
        result._name_bytes=_MappedNames(owner,28+bits,names)
        result._name_offsets=_MappedColumn(owner,28+bits+names,eligible+1,'Q')
        result._net_count=nets
        if result._name_offsets[0]!=0 or result._name_offsets[-1]!=names:
            raise ValidationError('native candidate name bounds disagree')
        result.implementation_id='native-candidate-ids-v1'
        return result
    except BaseException:
        owner.close()
        raise


def build_native_candidate_ids(ir):
    if sys.byteorder!='little':
        raise ValidationError('native candidate columns require little-endian storage')
    scratch=_ClusterScratch(prefix='phase3-native-candidates-')
    root=Path(scratch.name)
    try:
        request,output,report=root/'policy.bin',root/'ids.bin',root/'operation.json'
        generation=write_candidate_policy(request,ir)
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(report),'phase3','build-candidate-ids',str(ir.path),str(request),str(output)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),timeout_ms=21600000,scratch_parent=root) as process:
            completed=process.finish()
        if completed.returncode:
            raise ValidationError('native candidate construction failed: '+completed.stdout)
        if native_file_generation(ir.path)!=generation:
            raise ValidationError('native candidate source generation changed')
        if report.stat().st_size>65536:
            raise ValidationError('native candidate operation report exceeds bound')
        operation=json.loads(report.read_text())
        if (not isinstance(operation,dict) or operation.get('schema')!='emuflow.native-stage-result/v1'
                or operation.get('status')!='pass' or operation.get('stage')!='phase3.build-candidate-ids'
                or not isinstance(operation.get('metrics'),dict)):
            raise ValidationError('native candidate operation report disagrees')
        request.unlink(); report.unlink()
        result=_read_ids(output,scratch,len(ir.value['nets']))
        result.native_metrics=operation['metrics']
        return result
    except BaseException:
        scratch.cleanup()
        raise
