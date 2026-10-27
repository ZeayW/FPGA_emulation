"""Bounded requests for current assignment publication and rejection tests.

This driver encodes explicit fixture facts, not an alternate partition flow.
It has no selector transaction, fallback, graph analysis or resource policy.
"""
from array import array
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation, resolve_native_executable
from emuflow.native_protocol import CheckedRequestWriter
from emuflow.store_publication import (StoreSource, bind_store_source,
    require_store_sources_unchanged, validate_store_destination)
from emuflow.tool_runtime import NativeToolProcess


@dataclass(frozen=True)
class NativeAssignmentOwnership:
    sources: tuple
    parts: tuple
    column: object


@dataclass(frozen=True)
class NativeAssignmentCutSource:
    paths: tuple
    generations: tuple
    endpoint_counts: object
    auxiliary_sources: tuple = ()
    ownership: object = None


def native_assignment_cut_source(assignment):
    """Require exact original columns; never rebind a changed semantic source."""
    from emuflow.partition import _StaticExactCutNetRecords
    cuts=assignment.get('cut_nets');contract=assignment.get('semantic_contract')
    nodes=cuts._nodes if isinstance(cuts,_StaticExactCutNetRecords) else (contract or {}).get('cut_nodes')
    path=getattr(nodes,'native_semantic_artifact_path',None)
    if path is None:
        if getattr(nodes,'native_semantic_version',None) is not None:raise ValidationError('native assignment semantic artifact is missing')
        return None
    source=getattr(nodes,'direct_binary_source',None)
    if not isinstance(contract,dict) or contract.get('cut_nodes') is not nodes or source is None or getattr(nodes,'native_semantic_version',None)!=2:
        raise ValidationError('native assignment semantic owner is missing or mixed')
    paths=source.native_record_paths()
    counts=cuts._cuts._sink_endpoints if isinstance(cuts,_StaticExactCutNetRecords) else array('Q',(cut['sink_endpoints'] for cut in cuts))
    generations=(getattr(nodes,'native_semantic_generation',None),getattr(source.captures,'native_capture_generation',None))
    if (paths is None or Path(path).resolve()!=paths[0] or any(g is None for g in generations)
            or len(counts)!=len(cuts) or len(cuts)!=len(nodes) or counts.itemsize!=8 or sys.byteorder!='little'
            or len(cuts)>65536):raise ValidationError('native assignment semantic source dimensions disagree')
    if tuple(native_file_generation(p) for p in paths)!=generations:raise ValidationError('native assignment semantic generation changed')
    auxiliary=(counts.native_source,) if getattr(counts,'native_source',None) is not None else ()
    require_store_sources_unchanged(auxiliary)
    ownership=getattr(nodes,'native_assignment_ownership',None)
    if ownership is not None:
        selected=assignment.get('cluster_assignment');instance=assignment.get('instance_assignment')
        if (getattr(selected,'part_indices',None) is not ownership.column or getattr(selected,'part_ids',None)!=ownership.parts
                or ownership.sources[0].path!=Path(instance.path).resolve() or ownership.sources[0].generation!=instance._native_source_generation
                or ownership.sources[1].path!=Path(instance._native_primary_artifact).resolve() or ownership.sources[1].generation!=instance._native_primary_generation or not auxiliary):
            raise ValidationError('native assignment selected ownership is missing or mixed')
        require_store_sources_unchanged(ownership.sources);auxiliary+=ownership.sources[2:]
    return NativeAssignmentCutSource(paths,generations,counts,auxiliary,ownership)


def publish_native_assignment_index(path,ir,assignment,part_ids,metadata):
    from emuflow.phase3_assignment_index import _json, _normalized_cut_record
    instance=assignment['instance_assignment'];source=Path(instance.path)
    if len(instance)>65536:raise AssertionError('assignment publication fixture exceeds bound')
    if instance._native_primary_artifact is None:raise ValidationError('native assignment primary ownership is missing')
    primary=Path(instance._native_primary_artifact)
    if source!=Path(ir.path) or sys.byteorder!='little':raise ValidationError('native assignment publication source disagrees')
    refs=(StoreSource(source,'emuir-compact-v2',instance._native_source_generation),
        StoreSource(primary,'partition-cluster-columns-v2',instance._native_primary_generation))
    require_store_sources_unchanged(refs)
    if assignment.get('replication',{}).get('replicas'):raise ValidationError('fixture does not generate retired replica ownership')
    cuts=assignment['cut_nets'];typed=native_assignment_cut_source(assignment)
    borrowed=typed is not None and typed.ownership is not None
    with tempfile.TemporaryDirectory(prefix='assignment-request-facts-',dir=Path(path).parent) as temporary:
        root=Path(temporary);request=root/'request.bin';writer=CheckedRequestWriter(request)
        try:
            writer.put('QI',0x31514241554D5545,3 if borrowed else 2 if typed else 1)
            for ref in refs:writer.put('3Q4q',*ref.generation)
            if typed:writer.put('3Q4q',*typed.generations[0])
            if borrowed:
                for ref in typed.auxiliary_sources:writer.put('3Q4q',*ref.generation)
            writer.catalog(part_ids);writer.catalog(typed.ownership.parts if borrowed else instance.part_ids)
            writer.put('QQ',len(instance._cluster_parts),len(instance))
            if not borrowed:writer.bytes(array('I',instance._cluster_parts))
            writer.put('I',len(metadata))
            for key,value in sorted(metadata.items()):writer.text(key);writer.text(_json(value))
            writer.put('Q',len(cuts))
            if typed and not borrowed:writer.bytes(typed.endpoint_counts)
            if not typed:
                for raw in cuts:
                    row=_normalized_cut_record(raw,{part:i for i,part in enumerate(part_ids)})
                    writer.text(row['net']);writer.text(row['cut_class'])
                    writer.put('II',part_ids.index(row['source_fpgas'][0]),len(row['sink_fpgas']))
                    for part in row['sink_fpgas']:writer.put('I',part_ids.index(part))
                    writer.put('Qqqq',row['sink_endpoints'],row.get('transport_round',-1),row.get('dependency_level',-1),row.get('combinational_dependency_depth',-1))
                    writer.catalog(row.get('predecessor_cut_nets',()))
            writer.finish()
        finally:writer.close()
        tool=resolve_native_executable('emuflow-native')
        command=[tool,'phase3','publish-assignment',str(source),str(primary),str(request)]
        if typed:command.append(str(typed.paths[0]))
        if borrowed:command.extend(str(ref.path) for ref in typed.auxiliary_sources)
        command.append(str(path))
        with NativeToolProcess(tool,command,environment=dict(os.environ),scratch_parent=root,timeout_ms=60000) as process:result=process.finish()
        if result.returncode:raise ValidationError('native assignment publication failed: '+result.stdout)
        require_store_sources_unchanged(refs)
        if typed:
            if tuple(native_file_generation(p) for p in typed.paths)!=typed.generations:raise ValidationError('native assignment semantic generation changed')
            require_store_sources_unchanged(typed.auxiliary_sources)
        return dict(result.native_metrics,native_cut_records=len(cuts) if typed else 0,host_cut_records=0 if typed else len(cuts),
            host_ownership_records=0 if borrowed else len(instance._cluster_parts),cut_request_bytes=request.stat().st_size)


def publish_native_assignment_descriptor(path,ir,assignment,clusters,index_summary):
    from emuflow.phase3_assignment_index import _json
    instance=assignment['instance_assignment']
    if len(instance)>65536:raise AssertionError('descriptor fixture exceeds bound')
    refs=validate_store_destination(Path(path),(StoreSource(Path(instance.path).resolve(),'emuir-compact-v2',instance._native_source_generation),
        StoreSource(Path(instance._native_primary_artifact).resolve(),'partition-cluster-columns-v2',instance._native_primary_generation),
        bind_store_source(Path(path).with_name('assignment.sqlite'),'assignment-index-v4')))
    if Path(ir.path).resolve()!=refs[0].path:raise ValidationError('native assignment descriptor EmuIR disagrees')
    require_store_sources_unchanged(refs)
    contract=assignment['semantic_contract']
    if contract.get('schema')!='emuflow.static-exact-combinational-cut/v3' or len(clusters['clusters'])!=len(assignment['cluster_assignment']):raise ValidationError('native assignment descriptor owner disagrees')
    header={'document':{key:value for key,value in assignment.items() if key not in {'cluster_assignment','instance_assignment','partitions','semantic_contract','cut_nets'}},
        'partitions':[{key:value for key,value in part.items() if key!='clusters'} for part in assignment['partitions']],
        'semantic_contract_index':dict(artifact='assignment.sqlite',schema=contract['schema'],sha256=assignment['semantic_contract_sha256']),
        'cut_nets_index':dict(artifact='assignment.sqlite',schema='emuflow.phase3-cut-records/v1',records=index_summary['cut_records'],sha256=index_summary['cut_records_sha256'])}
    payload=_json(header)
    if len(payload.encode())>8*1024*1024-1024:raise ValidationError('descriptor fixture header exceeds bound')
    with tempfile.TemporaryDirectory(prefix='descriptor-request-facts-',dir=Path(path).parent) as temporary:
        request=Path(temporary)/'request.bin';writer=CheckedRequestWriter(request)
        try:
            writer.put('QI',0x31514441554D5545,1)
            for ref in refs:writer.put('3Q4q',*ref.generation)
            writer.text(payload);writer.finish()
        finally:writer.close()
        tool=resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool,[tool,'phase3','publish-assignment-descriptor',*(str(ref.path) for ref in refs),str(request),str(path)],
                environment=dict(os.environ),scratch_parent=Path(temporary),timeout_ms=60000) as process:result=process.finish()
        if result.returncode:raise ValidationError('native assignment descriptor publication failed: '+result.stdout)
        require_store_sources_unchanged(refs)
        return dict(result.native_metrics,request_bytes=request.stat().st_size)
