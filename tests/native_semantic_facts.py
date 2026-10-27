"""Bounded decoded facts from current native exact-node/capture artifacts.

No graph analysis, stream builders, lazy full-model views or canonical formatter.
These small projections exist only for native-kernel qualification assertions.
"""
import json
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.storage.store_publication import require_store_sources_unchanged

LIMIT = 65536
_ARRAYS = {'cut_nodes', 'dependency_edges', 'logic_segments', 'capture_requirements'}


class NativeFacts(list):
    def adopt_native_scratch(self, scratch):
        self._scratch = scratch

    def close(self):
        scratch = getattr(self, '_scratch', None)
        if scratch is not None:
            self._scratch = None
            scratch.cleanup()

    def __del__(self):
        self.close()


def _records(path, magic, versions, source=None):
    path = Path(path).resolve()
    generation = native_file_generation(path)
    if source is not None:
        if source.path != path or source.generation != generation:
            raise ValidationError('native fact source generation disagrees')
        require_store_sources_unchanged((source,))
    if path.stat().st_size > 32 * 1024 * 1024:
        raise AssertionError('native fact fixture exceeds byte bound')
    raw = path.read_bytes()
    try:
        actual, version, count, size = struct.unpack_from('<QIIQ', raw)
        start = 24 + 8 * (count + 1)
        if actual != magic or version not in versions or count > LIMIT or start + size != len(raw):
            raise ValueError('envelope')
        offsets = struct.unpack_from('<' + 'Q' * (count + 1), raw, 24)
        if offsets[0] or offsets[-1] != size or any(a >= b for a, b in zip(offsets, offsets[1:])):
            raise ValueError('offsets')
        if native_file_generation(path) != generation:
            raise ValueError('generation')
        return version, [raw[start+a:start+b] for a, b in zip(offsets, offsets[1:])], generation
    except (ValueError, struct.error) as error:
        raise ValidationError('native fact artifact is invalid') from error


def read_nodes(path, *, _validated_source=None):
    if _validated_source is not None and _validated_source.schema != 'exact-nodes-v2':
        raise ValidationError('native node source schema disagrees')
    version, records, generation = _records(path, 0x314E4553464D5545, (1, 2), _validated_source)
    result, flags, coordinates = NativeFacts(), [], []
    for raw in records:
        offset = 0
        def string():
            nonlocal offset
            size = struct.unpack_from('<I', raw, offset)[0]; offset += 4
            if not size or offset + size > len(raw):
                raise ValidationError('native node string is truncated')
            value = raw[offset:offset+size].decode('utf-8'); offset += size
            return value
        try:
            net, kind, source = string(), string(), string()
            level, depth, ns, np, nl, nc = struct.unpack_from('<6I', raw, offset); offset += 24
            if max(ns, np, nl, nc) > LIMIT:
                raise ValidationError('native node fixture exceeds relation bound')
            sinks, predecessors, launches, captures = [[string() for _ in range(n)] for n in (ns, np, nl, nc)]
            if version == 2:
                indices = struct.unpack_from('<'+'I'*np, raw, offset); offset += 4*np
                flag = raw[offset]; offset += 1
            else:
                indices, flag = (), int(not predecessors)
            if offset != len(raw) or flag not in (0, 1, 2):
                raise ValidationError('native node payload is invalid')
        except (IndexError, UnicodeDecodeError, struct.error) as error:
            raise ValidationError('native node payload is invalid') from error
        result.append(dict(net=net, cut_class=kind, source_fpgas=[source], sink_fpgas=sinks,
            dependency_level=level, combinational_dependency_depth=depth, predecessor_cut_nets=predecessors,
            source_segment_ids=launches, capture_segment_ids=captures))
        flags.append(flag); coordinates.append(indices)
    result.native_semantic_version = version
    result.native_semantic_artifact_path = Path(path).resolve()
    result.native_semantic_generation = generation
    result.native_launch_flag = flags.__getitem__
    result.native_dependency_indices = coordinates.__getitem__
    result.native_validation_source = _validated_source
    return result


def read_captures(path, *, _validated_source=None):
    if _validated_source is not None and _validated_source.schema != 'exact-captures-v1':
        raise ValidationError('native capture source schema disagrees')
    _, records, generation = _records(path, 0x31434553464D5545, (1,), _validated_source)
    result = NativeFacts()
    for index, raw in enumerate(records):
        try:
            kind, *sizes = struct.unpack_from('<B5I', raw); offset = 21
            fields = []
            for size in sizes:
                fields.append(raw[offset:offset+size]); offset += size
            if offset != len(raw) or kind not in (0, 1):
                raise ValueError('capture fields')
            net, part, endpoint, port = [field.decode('utf-8') for field in fields[:4]]
            row = dict(id=f'capture{index:06d}', cut_net=net, fpga=part,
                kind='architectural-state' if kind else 'top-output', endpoint=endpoint)
            if kind: row.update(port=port, bit=int.from_bytes(fields[4], 'big'))
            result.append(row)
        except (ValueError, struct.error) as error:
            raise ValidationError('native capture payload is invalid') from error
    result.native_capture_artifact_path = Path(path).resolve()
    result.native_capture_generation = generation
    return result


def semantic_arrays(nodes, captures, metrics, launches, *, require_source_proof=False):
    """Project native finalizer coordinates; no candidate/partition decisions."""
    flags = [nodes.native_launch_flag(i) for i in range(len(nodes))]
    if (require_source_proof and getattr(nodes, 'native_validation_source', None) is None
            or getattr(nodes, 'native_semantic_version', None) != 2
            or type(launches) is not int or not 0 <= launches <= len(nodes)
            or any(type(value) is not int or value < 0 for value in metrics.values())
            or metrics.get('transported_cut_nets') != len(nodes)
            or metrics.get('logic_segments') != launches + metrics.get('dependency_edges', -1) + metrics.get('capture_requirements', -1)
            or sum(flag != 0 for flag in flags) != launches or len(captures) != metrics['capture_requirements']):
        raise ValidationError('native semantic launch proof disagrees')
    edges, segments = [], []
    for node, flag in zip(nodes, flags):
        if flag:
            row = dict(id=node['source_segment_ids'][0], kind='launch_to_tx', fpga=node['source_fpgas'][0], sink_cut_net=node['net'])
            if flag == 2: row['source_semantics'] = 'configuration-stable-constant'
            segments.append(row)
    for node, flag in zip(nodes, flags):
        for pred, identity in zip(node['predecessor_cut_nets'], node['source_segment_ids'][int(flag != 0):]):
            edges.append({'from': pred, 'to': node['net'], 'segment': identity})
            segments.append(dict(id=identity, kind='rx_to_tx', fpga=node['source_fpgas'][0], source_cut_net=pred, sink_cut_net=node['net']))
    start = len(segments)
    for index, capture in enumerate(captures):
        segments.append(dict(id=f'segment{start+index:06d}', kind='rx_to_capture', fpga=capture['fpga'], source_cut_net=capture['cut_net'], capture_requirement=capture['id']))
    if len(edges) != metrics['dependency_edges'] or len(segments) != metrics['logic_segments']:
        raise ValidationError('native semantic record proof disagrees')
    source = SimpleNamespace(cut_nodes=nodes, captures=captures, dependency_edges=edges, logic_segments=segments)
    source.native_record_paths = lambda: (nodes.native_semantic_artifact_path, captures.native_capture_artifact_path)
    nodes.direct_binary_source = source
    return dict(dependency_edges=edges, logic_segments=segments)


def native_hash(nodes_path, captures_path, contract):
    """Encode only bounded JSON header tags for the retained C++ hash kernel."""
    if any(type(key) is not str for key in contract):
        raise ValidationError('native hash header key is invalid')
    tags = dict(capture_requirements=1, cut_nodes=2, dependency_edges=3, logic_segments=4)
    def text(value):
        raw = value.encode(); return struct.pack('<I', len(raw)) + raw
    request = struct.pack('<QII', 0x31484553464D5545, 1, len(contract))
    def check_header(value, depth=0):
        if depth > 32:
            raise ValidationError('native hash header depth exceeds bound')
        if isinstance(value, dict):
            if any(type(key) is not str for key in value):
                raise ValidationError('native hash header key is invalid')
            for item in value.values(): check_header(item, depth+1)
        elif isinstance(value, (list, tuple)):
            for item in value: check_header(item, depth+1)
    for key in sorted(contract):
        request += text(key) + bytes([tags.get(key, 0)])
        if key not in tags:
            check_header(contract[key])
            request += text(json.dumps(contract[key], indent=2, sort_keys=True).replace('\n', '\n  '))
    if len(request) > 1024*1024:
        raise AssertionError('native hash fixture exceeds header bound')
    with tempfile.TemporaryDirectory(prefix='native-hash-facts-') as temporary:
        root=Path(temporary); policy=root/'request'; output=root/'digest'; policy.write_bytes(request)
        result=subprocess.run([resolve_native_executable('emuflow-native'), 'phase3', 'hash-semantics', str(nodes_path), str(captures_path), str(policy), str(output)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValidationError('native canonical hashing failed: '+result.stderr)
        digest=output.read_text()
        if len(digest)!=65 or not digest.endswith('\n'):
            raise ValidationError('native canonical digest is invalid')
        return digest[:-1]
