"""Small binary-format facts and a bounded retained native-exporter driver.

This is an independent record encoder for reader/golden tests. It has no
section spools, direct topology serializer, publication transaction or flow.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

from emuflow.combinational_cut import semantic_contract_sha256
from emuflow.errors import ValidationError
from emuflow.phase3_exact_limits import STATIC_EXACT_CAPTURE_BIT_MAX_BITS, STATIC_EXACT_CAPTURE_BIT_ERROR


def validate_static_exact_capture_bit(value):
    if type(value) is not int or value < 0 or value.bit_length() > STATIC_EXACT_CAPTURE_BIT_MAX_BITS:
        raise ValidationError(STATIC_EXACT_CAPTURE_BIT_ERROR)
    return value


_ARRAYS = {'cut_nodes','dependency_edges','logic_segments','capture_requirements'}
_MISSING = 0xffffffff


def _string(value):
    data = value.encode('utf-8')
    return struct.pack('<I',len(data))+data


def _number(value):
    return int(value[7:])


def write_exact_records_fixture(path, contract, parts, *, semantic_digest=None, _format_version=2):
    nodes, segments, captures = (list(contract[key]) for key in ('cut_nodes','logic_segments','capture_requirements'))
    if max(len(nodes),len(segments),len(captures)) > 65536:
        raise ValidationError('exact format fixture exceeds record bound')
    if _format_version not in (1,2): raise ValueError('unknown exact format fixture version')
    nets = {node['net']:i for i,node in enumerate(nodes)}
    blobs = {'nets':b''.join(_string(node['net']) for node in nodes),
        'node_sources':b''.join(struct.pack('<I',len(node['source_segment_ids']))+
            b''.join(struct.pack('<I',_number(identity)) for identity in node['source_segment_ids']) for node in nodes)}
    counts = {'nets':len(nodes),'node_sources':len(nodes)}
    for i,part in enumerate(parts):
        records = [row for row in segments if row['fpga']==part]
        blobs[f'segments:{i}'] = b''.join(struct.pack('<IBBII',_number(row['id']),
            {'launch_to_tx':0,'rx_to_tx':1,'rx_to_capture':2}[row['kind']],
            int(row.get('source_semantics')=='configuration-stable-constant'),
            nets[row['source_cut_net']] if 'source_cut_net' in row else _MISSING,
            _number(row['capture_requirement']) if row['kind']=='rx_to_capture' else nets[row['sink_cut_net']]) for row in records)
        counts[f'segments:{i}'] = len(records)
        records = [row for row in captures if row['fpga']==part]
        data = bytearray()
        for row in records:
            state = row['kind']=='architectural-state'
            data.extend(struct.pack('<IIB',_number(row['id']),nets[row['cut_net']],int(state)))
            data.extend(_string(row['endpoint']))
            if state:
                data.extend(_string(row['port']))
                bit = validate_static_exact_capture_bit(row['bit'])
                if _format_version==1:
                    if bit>=_MISSING: raise ValidationError('Static Exact capture bit is invalid')
                    data.extend(struct.pack('<I',bit))
                else:
                    raw = bit.to_bytes(max(1,(bit.bit_length()+7)//8),'big')
                    data.extend(struct.pack('<I',len(raw))+raw)
        blobs[f'captures:{i}'] = bytes(data); counts[f'captures:{i}'] = len(records)
    payload = bytearray(); sections = {}
    for name,data in blobs.items():
        sections[name] = {'offset':28+len(payload),'bytes':len(data),'records':counts[name],
            'sha256':hashlib.sha256(data).hexdigest()}
        payload.extend(data)
    schema = f'emuflow.phase3-static-exact-binary/v{_format_version}'
    identity = semantic_digest or semantic_contract_sha256(contract)
    population = {'cut_nodes':len(nodes),'source_segments':sum(len(row['source_segment_ids']) for row in nodes),
        'logic_segments':len(segments),'consumer_segments':sum(isinstance(row.get('source_cut_net'),str) for row in segments),
        'capture_requirements':len(captures)}
    manifest = {'schema':schema,'semantic_contract_schema':contract.get('schema'),'semantic_contract_sha256':identity,
        'contract_header':{key:value for key,value in contract.items() if key not in _ARRAYS},
        'parts':list(parts),'counts':population,'sections':sections}
    if _format_version==2: manifest['capture_bit_max_bits'] = 8192
    encoded = json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(struct.pack('<8sIQQ',b'EMUEXACT',_format_version,28+len(payload),len(encoded))+payload+encoded)
    descriptor = {'artifact':'assignment.exact.bin','schema':schema,'bytes':path.stat().st_size,
        'manifest_sha256':hashlib.sha256(encoded).hexdigest(),'semantic_contract_schema':contract.get('schema'),
        'semantic_contract_sha256':identity,'counts':population}
    if _format_version==2: descriptor['capture_bit_max_bits'] = 8192
    return descriptor


def publish_exact_native_fixture(path, contract, parts, *, semantic_digest=None, _format_version=2):
    from emuflow.native_protocol import CheckedRequestWriter
    from emuflow.store_publication import StoreSource, require_store_sources_unchanged
    from emuflow.phase3_exact_native import _run
    nodes, captures = contract['cut_nodes'],contract['capture_requirements']
    source = nodes.direct_binary_source
    if (source is None or source.cut_nodes is not nodes or source.captures is not captures or
            source.logic_segments is not contract['logic_segments'] or source.dependency_edges is not contract['dependency_edges']):
        raise ValidationError('Static Exact direct binary generation is mixed')
    native_paths = source.native_record_paths()
    sources = (StoreSource(Path(native_paths[0]).resolve(),'exact-nodes-v2',nodes.native_semantic_generation),
        StoreSource(Path(native_paths[1]).resolve(),'exact-captures-v1',captures.native_capture_generation))
    require_store_sources_unchanged(sources)
    header = {key:value for key,value in contract.items() if key not in _ARRAYS}
    encoded = json.dumps(header,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>8*1024*1024-1024: raise ValidationError('exact fixture header exceeds bound')
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-exact-fixture-',dir=path.parent) as temporary:
        root = Path(temporary); request,selected = root/'request.bin',root/'selected'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII',0x31504245554D5545,1,_format_version)
            for item in sources: writer.put('3Q4q',*item.generation)
            writer.catalog(parts); writer.text(encoded); writer.text(semantic_digest or semantic_contract_sha256(contract)); writer.finish()
        finally: writer.close()
        _run(['phase3','publish-exact-binary',*(str(item.path) for item in sources),str(request),str(selected)],root)
        require_store_sources_unchanged(sources)
        descriptor = json.loads((selected/'descriptor.json').read_text())
        os.replace(selected/'assignment.exact.bin',path)
        return descriptor


def write_exact_fixture(path, contract, parts, **options):
    source = getattr(contract['cut_nodes'],'direct_binary_source',None)
    if source is not None and source.native_record_paths() is not None:
        return publish_exact_native_fixture(path,contract,parts,**options)
    return write_exact_records_fixture(path,contract,parts,**options)


def exact_partition_facts(path, descriptor, fpga):
    from emuflow.phase3_exact_binary import iter_phase3_exact_segments, iter_phase3_exact_captures
    return {'sha256':descriptor['semantic_contract_sha256'],
        'logic_segments':list(iter_phase3_exact_segments(path, descriptor, fpga)),
        'capture_requirements':list(iter_phase3_exact_captures(path, descriptor, fpga))}


def exact_contract_facts(path, descriptor, assignment):
    """Small independent record projection for canonical round-trip assertions."""
    from emuflow.phase3_exact_binary import (_load_manifest, iter_phase3_exact_node_sources,
        iter_phase3_exact_segments, iter_phase3_exact_captures)
    manifest = _load_manifest(path, descriptor)
    if max(manifest['counts'].values()) > 65536:
        raise AssertionError('exact contract fixture exceeds bound')
    segments = list(iter_phase3_exact_segments(path, descriptor))
    captures = list(iter_phase3_exact_captures(path, descriptor))
    cuts = {row['net']:row for row in assignment['cut_nets']}
    fields = {'net','cut_class','source_fpgas','sink_fpgas','dependency_level',
              'combinational_dependency_depth','predecessor_cut_nets'}
    nodes = [{**{key:cuts[net][key] for key in fields},
        'source_segment_ids':[f'segment{number:06d}' for number in numbers],
        'capture_segment_ids':sorted(row['id'] for row in segments if row['kind']=='rx_to_capture' and row['source_cut_net']==net)}
        for net,numbers in iter_phase3_exact_node_sources(path, descriptor)]
    result = {**manifest['contract_header'], 'cut_nodes':nodes, 'logic_segments':segments,
        'capture_requirements':captures, 'dependency_edges':sorted(({'from':row['source_cut_net'],
        'to':row['sink_cut_net'],'segment':row['id']} for row in segments if row['kind']=='rx_to_tx'),
        key=lambda row:(row['to'],row['from']))}
    if semantic_contract_sha256(result) != assignment['semantic_contract_sha256']:
        raise ValidationError('fixture canonical contract identity disagrees')
    return result


def indexed_exact_facts(path, assignment=None, fpga=None):
    from emuflow.phase3_assignment_index import open_phase3_assignment_index, _exact_binary_reference
    connection = open_phase3_assignment_index(path)
    try:
        binary, descriptor = _exact_binary_reference(connection, path)
        if fpga is not None:
            return exact_partition_facts(binary, descriptor, fpga)
        return exact_contract_facts(binary, descriptor, assignment)
    finally:
        connection.close()


def indexed_exact_manifest(path):
    """Read only the sealed exact-file header/counts for qualification checks."""
    from emuflow.phase3_assignment_index import open_phase3_assignment_index, _exact_binary_reference
    from emuflow.phase3_exact_binary import _load_manifest
    connection = open_phase3_assignment_index(path)
    try:
        binary, descriptor = _exact_binary_reference(connection, path)
        return _load_manifest(binary, descriptor)
    finally:
        connection.close()
