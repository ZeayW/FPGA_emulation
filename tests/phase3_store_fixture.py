
from emuflow.common.paths import DATA_ROOT
"""Small explicit Phase 3 store facts for reader and native-kernel tests.

No selector transaction, rollback, legacy flow, graph construction or search is
implemented here. Native complete-stage tests qualify production publication.
"""
from tests.phase3_exact_fixture import write_exact_fixture
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zlib

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, write_json
from emuflow.partition import phase3_assignment_index as index
from emuflow.storage.store_publication import StoreSource, require_store_sources_unchanged


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def cluster_document_fixture(value):
    if len(value['clusters']) > 65536:
        raise ValueError('cluster fixture exceeds bound')
    return dict(value, clusters=list(value['clusters']))


def assignment_selector_fixture(value, clusters_artifact, *, cut_nets_index=None):
    rows = clusters_artifact['clusters']
    if len(rows) > 65536:
        raise ValueError('assignment fixture exceeds bound')
    parts = sorted(part['fpga'] for part in value['partitions'])
    id_at = getattr(rows, 'cluster_id_at', None)
    ids = [id_at(i) for i in range(len(rows))] if callable(id_at) else [row['id'] for row in rows]
    width = 1 if len(parts) < 256 else 2 if len(parts) < 65536 else 4
    raw = b''.join(parts.index(value['cluster_assignment'][name]).to_bytes(width, 'little') for name in ids)
    contract = value.get('semantic_contract')
    digest = value.get('semantic_contract_sha256')
    if not isinstance(cut_nets_index, dict):
        raise ValueError('current assignment fixture requires typed cut index')
    excluded = {'cluster_assignment', 'instance_assignment', 'partitions', 'semantic_contract', 'cut_nets'}
    return {'schema':'emuflow.phase3-assignment-storage/v4',
        'document':{key:item for key,item in value.items() if key not in excluded},
        'cluster_count':len(ids), 'cluster_ids':None if ids == [f'c{i:06d}' for i in range(len(ids))] else ids,
        'part_ids':parts, 'cluster_parts':{'codec':'zlib-level-1-base64-unsigned-index', 'count':len(ids),
            'width':width, 'data':base64.b64encode(zlib.compress(raw, 1)).decode()},
        'partitions':[{key:item for key,item in part.items() if key!='clusters'} for part in value['partitions']],
        'semantic_contract':None,
        'semantic_contract_index':({'artifact':'assignment.sqlite','schema':contract['schema'],'sha256':digest} if contract is not None else None),
        'cut_nets_index':dict(cut_nets_index)}


def write_cluster_fixture(path, value, **_options):
    from tests.native_cluster_catalog_fixture import catalog_sources, build_native_cluster_catalog
    from emuflow.partition import phase3_storage as storage
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    rows, policy = value['clusters'], value['policy']
    if len(rows) > 65536: raise ValueError('cluster fixture exceeds bound')
    eligible = policy.get('eligible_combinational_cut_nets', [])
    document = {key:item for key,item in value.items() if key not in {'clusters','policy'}}
    document['policy'] = {key:item for key,item in policy.items() if key!='eligible_combinational_cut_nets'}
    native_sources = catalog_sources(rows, eligible)
    with tempfile.TemporaryDirectory(prefix='cluster-facts-', dir=path.parent) as temporary:
        store = Path(temporary)/'clusters.sqlite'
        if getattr(rows, 'native_artifact', None) is not None:
            counts = build_native_cluster_catalog(store, value, document, eligible,
                'eligible_combinational_cut_nets' in policy, native_sources)
        else:
            columns = {name:[] for name in storage._CLUSTER_SECTIONS}
            for seq, row in enumerate(rows):
                for name, data in (('cluster_ids',[row['id']]), ('member_counts',[len(row['instances'])]),
                    ('member_names',row['instances']), ('resource_counts',[len(row['resources'])]),
                    ('resource_names',sorted(row['resources'])), ('resource_values',[_json(row['resources'][name]) for name in sorted(row['resources'])]),
                    ('group_counts',[len(row['groups'])]), ('group_values',row['groups']),
                    ('extra_values',[_json({key:item for key,item in row.items() if key not in {'id','instances','resources','fixed_fpga','groups'}})])):
                    columns[name].extend(data)
                if row['fixed_fpga'] is not None:
                    columns['fixed_indices'].append(seq); columns['fixed_values'].append(row['fixed_fpga'])
            columns['eligible_nets'] = list(eligible); columns['document'] = [_json(document)]
            counts = {'clusters':len(rows),'instances':len(columns['member_names']),'eligible_nets':len(eligible)}
            with sqlite3.connect(store) as db:
                db.executescript(f'PRAGMA application_id={storage.CLUSTERS_SQLITE_APPLICATION_ID};PRAGMA user_version=1;'+(DATA_ROOT / 'schema/phase3_clusters.sql').read_text())
                for name, items in sorted(columns.items()):
                    integers = name in {'member_counts','resource_counts','fixed_indices','group_counts'}
                    raw = b''.join(item.to_bytes(4,'little') if integers else len(item.encode()).to_bytes(4,'little')+item.encode() for item in items)
                    data = zlib.compress(raw,1)
                    db.execute('INSERT INTO sections VALUES(?,?,?,?,?,?)',(name,'zlib-level-1-typed-columns',len(items),len(raw),len(data),1))
                    db.execute('INSERT INTO section_chunks VALUES(?,?,?)',(name,0,data))
                db.executemany('INSERT INTO metadata VALUES(?,?)',(('counts',_json(counts)),('eligible_nets_present',_json('eligible_combinational_cut_nets' in policy))))
        digest = file_sha256(store); target = path.with_name(f'clusters.{digest}.sqlite')
        os.replace(store,target)
    selector = {'schema':storage.INDEXED_CLUSTERS_SCHEMA,'logical_schema':value['schema'],'format':storage.CLUSTERS_SQLITE_FORMAT,
        'application_id':storage.CLUSTERS_SQLITE_APPLICATION_ID,'user_version':1,'artifact':target.name,
        'store_bytes':target.stat().st_size,'store_sha256':digest,'counts':counts}
    write_json(path,selector,compact=True)
    return selector


def write_assignment_index_fixture(path, ir, assignment):
    from emuflow.partition.phase3_exact_binary import (EXACT_BINARY_ARTIFACT)
    from emuflow.routing.routing import static_exact_contract_from_assignment
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    owners = assignment['instance_assignment']
    if len(owners) > 65536: raise ValidationError('assignment fixture exceeds instance bound')
    parts = sorted(part['fpga'] for part in assignment['partitions']); part_index = {part:i for i,part in enumerate(parts)}
    if any(part not in part_index for part in getattr(owners, 'part_ids', ())):
        raise ValidationError('assignment fixture targets an unknown FPGA')
    contract = static_exact_contract_from_assignment(assignment)
    native = getattr(owners, '_native_publication', False)
    sources = []
    if native:
        if owners._native_primary_artifact is None: raise ValidationError('native assignment primary ownership is missing')
        sources = [StoreSource(Path(owners.path), 'emuir-compact-v2', owners._native_source_generation),
            StoreSource(Path(owners._native_primary_artifact), 'partition-cluster-columns-v2', owners._native_primary_generation)]
        from tests.native_assignment_publication_fixture import native_assignment_cut_source
        cuts = native_assignment_cut_source(assignment)
        if cuts is not None:
            sources.extend(cuts.auxiliary_sources)
            sources.extend(StoreSource(p,s,g) for p,s,g in zip(cuts.paths,('exact-nodes-v2','exact-captures-v1'),cuts.generations))
    require_store_sources_unchanged(sources)
    with tempfile.TemporaryDirectory(prefix='assignment-facts-', dir=path.parent) as temporary:
        root = Path(temporary); target = root/path.name
        binary = write_exact_fixture(root/EXACT_BINARY_ARTIFACT, contract, parts,
            semantic_digest=assignment.get('semantic_contract_sha256')) if contract is not None else None
        metadata = {key:assignment.get(key) for key in ('design','platform','provider','seed','constraints')}
        metadata.update(schema=index.ASSIGNMENT_INDEX_SCHEMA,source_instance_count=len(ir.value['instances']),
            semantic_contract_sha256=assignment.get('semantic_contract_sha256') if contract is not None else None,
            semantic_contract_schema=contract.get('schema') if contract is not None else None,
            semantic_contract_header=({key:item for key,item in contract.items() if key not in {'cut_nodes','dependency_edges','logic_segments','capture_requirements'}} if contract is not None else None),semantic_contract_binary=binary)
        if native:
            from tests.native_assignment_publication_fixture import publish_native_assignment_index
            metrics = publish_native_assignment_index(target, ir, assignment, parts, metadata)
        else:
            with sqlite3.connect(target) as db:
                db.executescript(f'PRAGMA application_id={index.ASSIGNMENT_INDEX_APPLICATION_ID};PRAGMA user_version=4;'+index.CURRENT_DDL)
                db.executemany('INSERT INTO parts VALUES(?,?)',enumerate(parts))
                identities = {row['id']:seq for seq,row in enumerate(ir.value['instances'])}
                if len(identities)!=len(owners) or set(owners)!=set(identities): raise ValidationError('assignment fixture source coverage disagrees')
                if any(part not in part_index for part in owners.values()): raise ValidationError('assignment fixture targets an unknown FPGA')
                db.executemany('INSERT INTO copies VALUES(?,?,?,0)',((seq,part_index[owners[name]],name) for name,seq in identities.items()))
                for replica in (assignment.get('replication') or {}).get('replicas',[]):
                    db.executemany('INSERT INTO copies VALUES(?,?,?,1)',((identities[item['original_instance']],part_index[replica['target_fpga']],item['replica_instance']) for item in replica['instances']))
                digest = hashlib.sha256(); previous = None
                for seq,raw in enumerate(assignment['cut_nets']):
                    row = index._normalized_cut_record(raw,part_index)
                    if previous is not None and row['net']<=previous: raise ValidationError('cut-net assignment order is not canonical')
                    previous = row['net']; digest.update((index._json(row)+'\n').encode())
                    db.execute('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)',(seq,row['net'],part_index[row['source_fpgas'][0]],row['cut_class'],row['sink_endpoints'],row.get('transport_round'),row.get('dependency_level'),row.get('combinational_dependency_depth')))
                    db.executemany('INSERT INTO cut_sinks VALUES(?,?,?)',((row['net'],part_index[row['source_fpgas'][0]],part_index[sink]) for sink in row['sink_fpgas']))
                    db.executemany('INSERT INTO cut_predecessors VALUES(?,?,?)',((seq,i,net) for i,net in enumerate(row.get('predecessor_cut_nets',[]))))
                metadata.update(cut_records_schema=index.CUT_RECORDS_SCHEMA,cut_records=len(assignment['cut_nets']),cut_records_sha256=digest.hexdigest())
                db.executemany('INSERT INTO metadata VALUES(?,?)',((key,index._json(item)) for key,item in sorted(metadata.items())))
        summary = index.validate_phase3_assignment_index(target,expected_design=str(assignment.get('design')),expected_platform=str(assignment.get('platform')))
        require_store_sources_unchanged(sources)
        if binary is not None: os.replace(root/EXACT_BINARY_ARTIFACT,path.with_name(EXACT_BINARY_ARTIFACT))
        os.replace(target,path)
    summary['writer_implementation'] = 'native-primary-assignment-index-v1' if native else 'python-reference-v1'
    if native: summary['native_writer_metrics'] = metrics
    return summary


def write_current_assignment_fixture(path, ir, assignment):
    """Publish one small current typed input for downstream qualification."""
    path = Path(path)
    summary = write_assignment_index_fixture(path.with_name('assignment.sqlite'), ir, assignment)
    owners = assignment['cluster_assignment']
    if len(owners) > 65536:
        raise AssertionError('assignment selector fixture exceeds bound')
    clusters = {'clusters':[{'id':name} for name in sorted(owners)]}
    selector = assignment_selector_fixture(assignment,clusters,cut_nets_index={
        'artifact':'assignment.sqlite','schema':'emuflow.phase3-cut-records/v1',
        'records':summary['cut_records'],'sha256':summary['cut_records_sha256']})
    write_json(path,selector,compact=True)
    return summary


def cluster_column_facts(path):
    """Bounded logical column bytes for full catalog publication assertions."""
    from emuflow.common.io import read_json
    path = Path(path)
    selector = read_json(path)
    with sqlite3.connect(path.with_name(selector['artifact'])) as db:
        sections = db.execute('SELECT section,records,raw_bytes FROM sections ORDER BY section').fetchall()
        if any(records > 65536 for _,records,_ in sections) or sum(size for _,_,size in sections) > 32*1024*1024:
            raise AssertionError('cluster column fixture exceeds bound')
        result = {}
        for section,records,size in sections:
            compressed = b''.join(row[0] for row in db.execute(
                'SELECT payload FROM section_chunks WHERE section=? ORDER BY chunk_seq',(section,)))
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed,32*1024*1024+1)
            if len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise AssertionError('cluster column fixture payload disagrees')
            result[section] = (records,raw)
    return result
