"""Bounded protocol drivers for retained current native partition kernels.

No graph construction, optimizer or production flow is implemented in Python.
The tests exercise original source receipts, explicit request corruption and
independent native audits. Production calls only the complete phase owner.
"""
from array import array
from dataclasses import dataclass
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import zlib

from emuflow.common.errors import ValidationError
from tests.native_column_fixture import read_fixture_columns, _ClusterScratch, _ColumnOwner, _MappedColumn, _MappedNames
from emuflow.common.native_tools import native_file_generation, resolve_native_executable, require_native_schema
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_protocol import CheckedRequestWriter as _Writer
from emuflow.ir.resources import RESOURCE_FIELDS
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess
from tests.phase3_policy_reference import write_candidate_policy
from emuflow.partition.structural_policy import _pattern_range_policy
from tests.partition_views import ClusterColumns, ClusterOwners, CandidateColumns, require_coverage


LIMIT=65536


def _read_ids(path, scratch, expected_nets):
    data=read_fixture_columns(path)
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
        result=object.__new__(CandidateColumns)
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
    if len(ir.value['instances'])>LIMIT or len(ir.value['nets'])>LIMIT:raise AssertionError('candidate fixture exceeds bound')
    if sys.byteorder!='little':
        raise ValidationError('native candidate columns require little-endian storage')
    scratch=_ClusterScratch(prefix='phase3-native-candidates-')
    root=Path(scratch.name)
    try:
        request,output,report=root/'policy.bin',root/'ids.bin',root/'operation.json'
        generation=write_candidate_policy(request,ir)
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(report),'phase3','build-candidate-ids',str(ir.path),str(request),str(output)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),timeout_ms=60000,scratch_parent=root) as process:
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
        result.native_source_path = Path(ir.path)
        result.native_artifact = output
        result.native_source_generation = generation
        result.native_generation = native_file_generation(output)
        return result
    except BaseException:
        scratch.cleanup()
        raise


def _load_columns(output, scratch, expected_cells, fixed, groups, generation):
    data = read_fixture_columns(output)
    owner = _ColumnOwner(data,scratch)
    try:
        if len(data) < 204:
            raise ValidationError('native cluster columns are truncated')
        magic, version, cells, clusters, names = struct.unpack_from('<QIQQQ', data)
        expected = 200 + 8*(cells+1) + 4*(clusters+1) + 8*cells + 48*clusters + names + 4
        if (magic != 0x314C4350464D5545 or version != 2 or cells != expected_cells
                or clusters > cells or len(data) != expected):
            raise ValidationError('native cluster columns have invalid dimensions')
        crc = 0
        for offset in range(0, len(data)-4, 1024*1024):
            crc = zlib.crc32(data[offset:min(offset+1024*1024,len(data)-4)], crc)
        if crc != struct.unpack_from('<I', data, len(data)-4)[0]:
            raise ValidationError('native cluster columns checksum disagrees')
        if struct.unpack_from('<3Q4q', data, 36) != generation:
            raise ValidationError('native cluster source generation disagrees')
        totals = struct.unpack_from('<13Q', data, 92)
        if zlib.crc32(data[:196]) != struct.unpack_from('<I', data, 196)[0] or totals[0] != cells:
            raise ValidationError('native cluster header totals/checksum disagree')
        offset = 200
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
        result = ClusterColumns(name_bytes=name_bytes, name_offsets=name_offsets,
            member_offsets=member_offsets, member_ordinals=members, resource_columns=resources,
            source_cluster_indices=owners, fixed_by_cluster=fixed_by_cluster,
            groups_by_cluster={key:sorted(value) for key,value in groups_by_cluster.items()})
        result.implementation_id = 'native-primary-clusters-v1'
        result.native_resource_totals = totals
        return result
    except BaseException:
        owner.close()
        raise


def build_native_clusters(ir, constraints, eligible):
    if len(ir.value['instances'])>LIMIT or len(ir.value['nets'])>LIMIT:raise AssertionError('cluster fixture exceeds bound')
    from emuflow.ir.emuir_storage import open_emuir_sqlite_read_only
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
        with NativeToolProcess(executable,command,environment=dict(os.environ),timeout_ms=60000,
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
        result = _load_columns(output,scratch,cells,fixed,groups,generation)
        result.native_metrics = operation['metrics']
        result.native_source_path = source
        result.native_artifact = output
        result.native_generation = native_file_generation(output)
        result.native_source_generation = generation
        return result
    except BaseException:
        scratch.cleanup()
        raise


def _ownership_sources(ir,records,assignment):
    if getattr(records,'native_artifact',None) is None:return None
    source=StoreSource(Path(ir.path).resolve(),'emuir-compact-v2',records.native_source_generation)
    primary=StoreSource(Path(records.native_artifact).resolve(),'partition-cluster-columns-v2',records.native_generation)
    for prefix,kind in (('native_output',2),('native_initial',1)):
        selected=getattr(assignment,prefix+'_source',None)
        if selected is None:continue
        if (assignment.part_indices is not getattr(assignment,prefix+'_column',None)
                or assignment.part_ids!=getattr(assignment,prefix+'_parts',None)
                or assignment.native_primary_source!=primary or assignment.native_emuir_source!=source):
            raise ValidationError('native final ownership belongs to another source or column')
        refs=[source,primary,selected]
        if kind==2:refs.append(assignment.native_order_source)
        require_store_sources_unchanged(refs)
        return kind,source,primary,refs[2:]
    # One active FPGA has no graph-sized partition decision to serialize.
    if len(getattr(assignment,'part_ids',()))==1:
        return 0,source,primary,[]
    return None


def _policy_snapshot(platform, constraints):
    return json.dumps({'constraints': constraints,
        'fpgas': [(fpga.id, fpga.effective_capacity) for fpga in platform.fpgas]},
        sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class _OwnershipAudit:
    facts: dict
    sources: tuple
    owner: object
    policy: str

    def require(self, platform, assignment):
        if self.owner is not assignment['cluster_assignment'] or self.policy != _policy_snapshot(platform, assignment['constraints']):
            raise ValidationError('native partition audit belongs to another ownership or policy')
        require_store_sources_unchanged(self.sources)
        return self.facts


def ownership_sources(records, assignment):
    source = getattr(records, 'native_source_path', None)
    if source is None:
        return None
    owner = _ownership_sources(SimpleNamespace(path=source), records, assignment)
    return None if owner is not None and owner[0] == 0 else owner


def _run(platform, clusters, assignments):
    if len(clusters['clusters'])>LIMIT:raise AssertionError('decision fixture exceeds bound')
    records = clusters['clusters']
    owners = [ownership_sources(records, value['cluster_assignment']) for value in assignments]
    if any(owner is None for owner in owners):
        return None  # Explicit reference/diagnostic inputs have no native owner.
    if len(owners) == 2 and [owner[0] for owner in owners] != [1, 2]:
        return None
    first = owners[0]
    if any(owner[1:3] != first[1:3] for owner in owners):
        raise ValidationError('native partition decision belongs to mixed primary sources')
    constraints = assignments[0]['constraints']
    if any(value['constraints'] != constraints for value in assignments):
        raise ValidationError('native partition decision has different constraints')
    sources = [first[1], first[2], *(item for owner in owners for item in owner[3])]
    kind = 3 if len(owners) == 2 else first[0]
    require_native_schema('partition-decision-request-v1')
    from emuflow_tools.partition.partition_limits import planning_resource_capacities
    from emuflow.ir.resources import RESOURCE_FIELDS
    parts = tuple(fpga.id for fpga in platform.fpgas)
    planning = planning_resource_capacities(platform.fpgas, constraints.get('resource_reservations_by_fpga', {}))
    dimensions = ('cells', *RESOURCE_FIELDS)
    balanced = constraints.get('balance_dimensions')
    explicit = json.dumps({key: constraints.get(key, []) for key in ('groups', 'fixed')},
                          sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(explicit.encode()) > 8 * 1024 * 1024 - 65536:
        raise ValidationError('native partition fixed/group request exceeds bound')
    with tempfile.TemporaryDirectory(prefix='emuflow-partition-decision-') as temporary:
        root = Path(temporary)
        request, output, operation = root/'request.bin', root/'summary.json', root/'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31514450464D5545, 1, kind)
            for source in sources:
                writer.put('3Q4q', *source.generation)
            for value in assignments:
                writer.catalog(value['cluster_assignment'].part_ids)
            writer.catalog(parts)
            writer.catalog(constraints.get('active_fpgas', parts))
            writer.put('Q', constraints.get('min_used_fpgas', 1))
            for field in dimensions:
                writer.put('Bd', int(balanced is None or field in balanced),
                           constraints.get('balance_tolerance_by_dimension', {}).get(field, constraints['balance_tolerance']))
            for fpga in platform.fpgas:
                for field in RESOURCE_FIELDS:
                    writer.put('QQ', fpga.effective_capacity.get(field, 0), planning[fpga.id].get(field, 0))
            writer.text(explicit)
            if kind == 3:
                for value in assignments:
                    writer.text(value['provider'])
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        tool = resolve_native_executable('emuflow-native')
        arguments = [str(sources[0].path), str(sources[1].path), str(request),
                     *(str(source.path) for source in sources[2:]), str(output)]
        with NativeToolProcess(tool, [tool, '--report', str(operation), 'phase3', 'audit-decision', *arguments],
                               environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition decision failed: ' + completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256 * 1024:
            raise ValidationError('native partition decision operation exceeds bound')
        proof = json.loads(operation.read_text())
        outputs = proof.get('outputs')
        if proof.get('stage') != 'phase3.audit-decision' or proof.get('status') != 'pass' or not isinstance(outputs, list) or len(outputs) != 1:
            raise ValidationError('native partition decision producer result disagrees')
        record = outputs[0]
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        generation = record.get('generation', {})
        if record.get('path') != str(output.resolve()) or record.get('schema') != 'partition-decision-summary-v1' or any(type(generation.get(key)) is not int for key in fields):
            raise ValidationError('native partition decision output binding disagrees')
        original = StoreSource(output.resolve(), 'partition-decision-summary-v1', tuple(generation[key] for key in fields))
        require_store_sources_unchanged((original,))
        if output.stat().st_size > 16384 + len(parts) * 4096:
            raise ValidationError('native partition decision summary exceeds bound')
        result = json.loads(output.read_text())
        require_store_sources_unchanged((*sources, original))
    if (result.get('schema') != 'emuflow.partition-decision/v1' or result.get('status') != 'pass'
            or result.get('implementation_id') != 'native-partition-decision-v1'
            or not isinstance(result.get('audits'), list) or len(result['audits']) != len(assignments)):
        raise ValidationError('native partition decision summary disagrees')
    for audit in result['audits']:
        if (audit.get('schema') != 'emuflow.partition-load-summary/v1' or audit.get('parts') != list(parts)
                or audit.get('clusters') != len(records) or audit.get('instances') != len(records.source_cluster_indices)):
            raise ValidationError('native partition decision population disagrees')
    snapshot = _policy_snapshot(platform, constraints)
    result['native_audits'] = tuple(_OwnershipAudit(audit, tuple(sources), value['cluster_assignment'], snapshot)
                                  for audit, value in zip(result['audits'], assignments))
    result['native_metrics'] = completed.native_metrics
    return result


def audit_assignment(platform, clusters, assignment):
    result = _run(platform, clusters, (assignment,))
    return None if result is None else result['native_audits'][0]


def select_assignment(platform, clusters, initial, candidate):
    result = _run(platform, clusters, (initial, candidate))
    if result is None:
        return None
    selection = result.get('selection', {})
    if selection.get('status') != 'pass' or selection.get('selected') not in {'initial', 'candidate'}:
        raise ValidationError('native partition candidate selection disagrees')
    return result


def reduce_native_partition_loads(clusters, assignment, fpga_ids, *, metrics=None):
    if len(clusters)>LIMIT:raise AssertionError('load fixture exceeds bound')
    require_coverage(clusters, assignment)
    if sys.byteorder != 'little':
        raise ValidationError('native partition loads require little-endian storage')
    source = getattr(clusters, 'native_source_path', None)
    primary = getattr(clusters, 'native_artifact', None)
    if source is None or primary is None:
        raise ValidationError('native primary artifact reference is unavailable')
    source, primary = Path(source), Path(primary)
    generations = (clusters.native_source_generation, clusters.native_generation)
    if tuple(native_file_generation(path) for path in (source, primary)) != generations:
        raise ValidationError('native partition load source generation changed')
    owned = ownership_sources(clusters, assignment)
    if isinstance(assignment, ClusterOwners):
        parts, indices = assignment.part_ids, assignment.part_indices
    else:
        parts = tuple(fpga_ids)
        lookup = {part: index for index, part in enumerate(parts)}
        try:
            indices = array('I', (lookup[assignment[f'c{i:06d}']] for i in range(len(clusters))))
        except KeyError as error:
            raise ValidationError('balance assignment references unknown FPGA') from error
    if not parts or len(parts) > 65536 or indices.itemsize != 4:
        raise ValidationError('native partition load ownership dimensions disagree')
    with tempfile.TemporaryDirectory(prefix='emuflow-partition-loads-') as temporary:
        root = Path(temporary)
        request, output = root/'request.bin', root/'loads.json'
        writer = _Writer(request)
        try:
            if owned is not None:
                writer.put('QI', 0x3251454C504D5545, 2)
                for item in (owned[1], owned[2], *owned[3]):
                    writer.put('3Q4q', *item.generation)
                writer.put('I', len(parts))
                for part in parts:
                    writer.text(part)
            else:
                writer.put('QI', 0x3151454C504D5545, 1)
                for generation in generations:
                    writer.put('3Q4q', *generation)
                writer.put('QI', len(clusters), len(parts))
                for part in parts:
                    writer.text(part)
                writer.bytes(indices)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        owner_sources = () if owned is None else tuple(owned[3])
        command = [executable, 'phase3', 'reduce-loads', str(source), str(primary), str(request),
                   *(str(item.path) for item in owner_sources), str(output)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               timeout_ms=60000, scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition load reduction failed: '+completed.stdout)
        if tuple(native_file_generation(path) for path in (source, primary)) != generations:
            raise ValidationError('native partition load source generation changed')
        from emuflow.storage.store_publication import require_store_sources_unchanged
        require_store_sources_unchanged(owner_sources)
        if output.stat().st_size > 4096+len(parts)*512:
            raise ValidationError('native partition load summary exceeds bound')
        summary = json.loads(output.read_text())
        if not isinstance(summary, dict):
            raise ValidationError('native partition load summary disagrees')
        if metrics is not None:
            metrics.update(completed.native_metrics)
    if owned is not None and summary.get('parts') != list(parts):
        raise ValidationError('native partition load part catalog disagrees')
    return decode_partition_load_summary(summary, clusters, parts, fpga_ids)


def decode_partition_load_summary(summary, clusters, parts, fpga_ids):
    def integers(values, count):
        return (isinstance(values, list) and len(values) == count and
                all(type(value) is int and value >= 0 for value in values))
    loads, counts, totals = (summary.get(key) for key in ('loads', 'cluster_counts', 'totals'))
    if (summary.get('schema') != 'emuflow.partition-load-summary/v1' or
            summary.get('clusters') != len(clusters) or
            summary.get('instances') != len(clusters.source_cluster_indices) or
            not integers(totals, 13) or not integers(counts, len(parts)) or
            not isinstance(loads, list) or len(loads) != len(parts) or
            not all(integers(row, 13) for row in loads) or
            sum(counts) != len(clusters) or totals[0] != summary['instances'] or
            any(sum(row[d] for row in loads) != totals[d] for d in range(13))):
        raise ValidationError('native partition load summary disagrees')
    allowed = set(fpga_ids)
    if any(count for part, count in zip(parts, counts) if part not in allowed):
        raise ValidationError('balance assignment references unknown FPGA')
    dimensions = ('cells', *RESOURCE_FIELDS)
    loads_by_part = {part: dict.fromkeys(dimensions, 0) for part in fpga_ids}
    counts_by_part = dict.fromkeys(fpga_ids, 0)
    for part, row, count in zip(parts, loads, counts):
        if part in allowed:
            loads_by_part[part] = dict(zip(dimensions, row))
            counts_by_part[part] = count
    return dict(zip(dimensions, totals)), loads_by_part, counts_by_part


class NativePartitionConstraints(dict):
    implementation_id = 'native-partition-configuration-v1'


def prepare_configuration(source_path, platform, value, *, min_used_fpgas=None,
                          balance_tolerance=None, constraints_path=None):
    require_native_schema('partition-configuration-request-v1')
    source = bind_store_source(Path(source_path).resolve(), 'emuir-compact-v2')
    configuration = {
        'pattern_range_policy': _pattern_range_policy(),
        'min_used_fpgas': min_used_fpgas,
        'balance_tolerance': balance_tolerance,
        'board': {
            'fpgas': [{'id': fpga.id, 'effective_capacity': fpga.effective_capacity}
                      for fpga in platform.fpgas],
            'links': [{'from': link.endpoints[0], 'to': link.endpoints[1],
                       'capacity': link.transport_bits_per_cycle_per_direction,
                       'latency': link.latency_cycles,
                       'bidirectional': link.direction in {'full_duplex', 'half_duplex'}}
                      for link in platform.links],
        },
    }
    try:
        payload = json.dumps(configuration, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValidationError('partition configuration is not finite JSON metadata') from error
    if len(payload.encode()) > 8 * 1024 * 1024:
        raise ValidationError('partition configuration exceeds bounded metadata contract')
    with tempfile.TemporaryDirectory(prefix='emuflow-partition-configuration-') as temporary:
        root = Path(temporary)
        request, output, operation = root/'request.bin', root/'constraints.json', root/'operation.json'
        raw_source = None
        if constraints_path is not None:
            if value is not None:
                raise ValidationError('partition configuration has two raw input owners')
            raw_source = bind_store_source(Path(constraints_path).resolve(), 'partition-constraints-input-v1')
        elif value is not None:
            raw = root/'raw.json'
            try:
                with raw.open('w', encoding='utf-8') as stream:
                    json.dump(value, stream, sort_keys=True, separators=(',', ':'), allow_nan=False)
            except (TypeError, ValueError, UnicodeError) as error:
                raise ValidationError('partition constraints are not finite UTF-8 JSON') from error
            raw_source = bind_store_source(raw, 'partition-constraints-input-v1')
        sources = (source,) if raw_source is None else (source, raw_source)
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31474350464D5545, 1, int(raw_source is not None))
            for item in sources:
                writer.put('3Q4q', *item.generation)
            writer.text(payload)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        with NativeToolProcess(executable, [executable, '--report', str(operation),
                'phase3', 'normalize-configuration', str(source.path), str(request),
                *(() if raw_source is None else (str(raw_source.path),)), str(output)],
                environment=dict(os.environ), timeout_ms=60000, scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition configuration failed: ' + completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256 * 1024:
            raise ValidationError('native partition configuration result exceeds bound')
        proof = json.loads(operation.read_text())
        outputs = proof.get('outputs')
        if proof.get('stage') != 'phase3.normalize-configuration' or proof.get('status') != 'pass' or not isinstance(outputs, list) or len(outputs) != 1:
            raise ValidationError('native partition configuration producer result disagrees')
        record = outputs[0]
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        generation = record.get('generation', {})
        if record.get('path') != str(output.resolve()) or record.get('schema') != 'partition-constraints-v1' or any(type(generation.get(key)) is not int for key in fields):
            raise ValidationError('native partition configuration original output binding disagrees')
        published = StoreSource(output.resolve(), 'partition-constraints-v1', tuple(generation[key] for key in fields))
        require_store_sources_unchanged((published, *sources))
        if output.stat().st_size > 8 * 1024 * 1024 + 1:
            raise ValidationError('native normalized configuration exceeds bound')
        result = json.loads(output.read_text())
        require_store_sources_unchanged((published, *sources))
        if not isinstance(result, dict) or result.get('schema') != 'emuflow.partition-constraints/v1':
            raise ValidationError('native normalized configuration schema disagrees')
        result = NativePartitionConstraints(result)
        result.native_metrics = completed.native_metrics
        result.pattern_range_policy = configuration['pattern_range_policy']
        return result
