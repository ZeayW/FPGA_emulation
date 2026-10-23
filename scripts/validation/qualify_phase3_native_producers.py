#!/usr/bin/env python3
"""Offline, one-shot differential of real candidate and primary-cluster producers.

No partition search, placement, routing or full-flow qualification is implied.
Both arms consume the same sealed immutable input in separate worker processes.
Only compact column identities and measurements survive terminal cleanup.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time

from emuflow.errors import ValidationError
from emuflow.io import read_json,write_json
from emuflow.native_tools import native_file_generation,resolve_native_executable
from emuflow.partition import normalize_partition_constraints,_build_clusters_from_compact_ir
from emuflow.partition_ir import load_partition_ir
from emuflow.platform import Platform
from emuflow.resources import RESOURCE_FIELDS
from emuflow.stage_metrics import measure_stage
from emuflow.tool_runtime import NativeToolProcess


def digest_bytes(column):
    digest=hashlib.sha256()
    chunks=getattr(column,'iter_bytes',None)
    if chunks is not None:
        for block in chunks():digest.update(block)
    else:
        with memoryview(column).cast('B') as raw:
            for start in range(0,len(raw),1024*1024):digest.update(raw[start:start+1024*1024])
    return digest.hexdigest()


def file_digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def cancel(signum,_frame):
    raise KeyboardInterrupt(f'native producer qualification cancelled by {signum}')


def worker(request_path):
    request=read_json(request_path)
    if request.get('schema')!='emuflow.phase3-native-producer-worker/v1':
        raise ValidationError('unknown native producer qualification request')
    arm=request['implementation'];output=Path(request['output']);source=Path(request['source'])
    if arm not in {'reference','native'}:raise ValidationError('unknown qualification implementation')
    scratch=output/'tmp';scratch.mkdir()
    for name in ('TMPDIR','TMP','TEMP','SQLITE_TMPDIR','XDG_CACHE_HOME'):os.environ[name]=str(scratch)
    tempfile.tempdir=None
    if list(native_file_generation(source))!=request['generation']:
        raise ValidationError('qualification input generation changed')
    ir=load_partition_ir(source)
    constraints=read_json(Path(request['constraints']))
    if ir.value['design']['name']!=request['design'] or len(ir.value['instances'])<request['minimum_instances']:
        raise ValidationError('qualification input design/scale disagrees')
    if arm=='reference':
        from emuflow.combinational_cut import _compact_combinational_cut_candidate_ids
        candidates=lambda:_compact_combinational_cut_candidate_ids(ir,source)
        clusters=lambda ids:_build_clusters_from_compact_ir(ir,constraints,ids)
    else:
        from emuflow.phase3_candidates_native import build_native_candidate_ids
        from emuflow.phase3_clusters_native import build_native_clusters
        candidates=lambda:build_native_candidate_ids(ir)
        clusters=lambda ids:build_native_clusters(ir,constraints,ids)
    started=time.process_time()
    ids=measure_stage(output,'candidate-production',candidates)
    candidate_cpu=time.process_time()-started;started=time.process_time()
    primary=measure_stage(output,'primary-clustering',clusters,ids)
    cluster_cpu=time.process_time()-started
    # Hash the complete independent physical representations, not sampled names,
    # a summary count, or a shared producer decision copied into its validator.
    columns={
        'candidate.bits':ids._ordinal_bits,'candidate.names':ids._name_bytes,
        'candidate.offsets':ids._name_offsets,
        'cluster.names':primary._name_bytes,'cluster.name_offsets':primary._name_offsets,
        'cluster.member_offsets':primary._member_offsets,'cluster.members':primary._member_ordinals,
        'cluster.source_owners':primary.source_cluster_indices,
    }
    columns.update({f'cluster.resources.{name}':column for name,column in zip(RESOURCE_FIELDS,primary._resource_columns)})
    identities={name:digest_bytes(column) for name,column in columns.items()}
    constraints_identity=json.dumps({'fixed':primary._fixed_by_cluster,'groups':primary._groups_by_cluster},sort_keys=True,separators=(',',':')).encode()
    identities['cluster.fixed_groups']=hashlib.sha256(constraints_identity).hexdigest()
    if list(native_file_generation(source))!=request['generation']:
        raise ValidationError('qualification input generation changed during production')
    write_json(output/'result.json',{
        'schema':'emuflow.phase3-native-producer-arm/v1','status':'pass','implementation':arm,
        'counts':{'instances':len(ir.value['instances']),'nets':len(ir.value['nets']),'eligible':len(ids),'clusters':len(primary)},
        'column_sha256':identities,
        'candidate_metrics':read_json(output/'stage-metrics/candidate-production.json'),
        'cluster_metrics':read_json(output/'stage-metrics/primary-clustering.json'),
        'parent_cpu_seconds':{'candidate':candidate_cpu,'cluster':cluster_cpu},
        'native_operation_metrics':{'candidate':getattr(ids,'native_metrics',None),'cluster':getattr(primary,'native_metrics',None)},
    })


def qualify(args):
    output=args.out.resolve();source=args.ir.resolve();platform=args.platform.resolve()
    if output.exists():raise ValidationError('qualification output must be new')
    native=Path(resolve_native_executable('emuflow-native'))
    install=Path(os.environ['EMUFLOW_NATIVE_ROOT']) if os.environ.get('EMUFLOW_NATIVE_ROOT') else native.parent.parent
    source_identity={'declared_commit':args.source_commit,'native_binary_sha256':file_digest(native)}
    if args.source_commit:
        if (install/'SOURCE_COMMIT').read_text().strip()!=args.source_commit:
            raise ValidationError('qualification source commit disagrees with runtime')
        import emuflow
        if Path((install/'PYTHON_SOURCE_PATH').read_text().strip()).resolve()!=Path(emuflow.__file__).resolve().parent.parent:
            raise ValidationError('qualification Python source disagrees with runtime')
        built=install/'NATIVE_BUILD_SOURCE_COMMIT'
        source_identity['native_build_commit']=built.read_text().strip() if built.exists() else args.source_commit
    generation=list(native_file_generation(source));source_sha=file_digest(source)
    if list(native_file_generation(source))!=generation:raise ValidationError('qualification source changed while sealing')
    ir=load_partition_ir(source)
    if ir.value['design']['name']!=args.expected_design or len(ir.value['instances'])<args.minimum_instances:
        raise ValidationError('qualification design/scale disagrees')
    constraints=normalize_partition_constraints(read_json(args.constraints) if args.constraints else None,ir,Platform.load(platform))
    output.mkdir(parents=True)
    scratch=output/'tmp';scratch.mkdir()
    normalized=scratch/'constraints.json';write_json(normalized,constraints)
    common={'schema':'emuflow.phase3-native-producer-worker/v1','source':str(source),'generation':generation,
        'constraints':str(normalized),'design':args.expected_design,'minimum_instances':args.minimum_instances}
    result={'schema':'emuflow.phase3-native-producer-differential/v1','status':'failed',
        'scope':'candidate-and-primary-cluster-producers-only; no search/full-flow/physical qualification',
        'source':source_identity,'input':{'design':args.expected_design,'sha256':source_sha,'generation':generation},
        'configuration':{'platform_sha256':file_digest(platform),'normalized_constraints_sha256':file_digest(normalized)},
        'arm_order':['reference','native'],'source_hash_precedes_arms':True,
        'timing_excludes_column_hashing':True,'arms':{}}
    try:
        for arm in ('reference','native'):
            arm_root=output/arm;arm_root.mkdir()
            request=scratch/f'{arm}.json';write_json(request,dict(common,implementation=arm,output=str(arm_root)))
            command=[os.path.abspath(sys.executable),str(Path(__file__).resolve()),'--worker-request',str(request)]
            with NativeToolProcess(str(native),command,environment=dict(os.environ),scratch_parent=scratch,
                                   timeout_ms=21600000,termination_grace_ms=10000) as process:
                completed=process.finish()
            if completed.returncode:raise ValidationError(f'{arm} producer qualification failed: '+completed.stdout)
            result['arms'][arm]=read_json(arm_root/'result.json')
            result['arms'][arm]['worker_metrics']=completed.native_metrics
            # The worker and its nested tool owners have exited before retiring
            # its mapped circuit payloads. Inputs outside this attempt are read-only.
            shutil.rmtree(arm_root/'tmp')
        left,right=result['arms']['reference'],result['arms']['native']
        if left['counts']!=right['counts'] or left['column_sha256']!=right['column_sha256']:
            result['mismatched_columns']=[name for name,value in left['column_sha256'].items() if right['column_sha256'].get(name)!=value]
            raise ValidationError('native producer records differ from independent reference')
        result['status']='pass'
        return result
    except BaseException as error:
        result['error']=str(error)[-32768:]
        raise
    finally:
        # NativeToolProcess contexts complete group cleanup before this path.
        try:
            for arm in ('reference','native'):
                path=output/arm/'tmp'
                if path.exists():shutil.rmtree(path)
            shutil.rmtree(scratch)
        except OSError as error:
            result['status']='failed';result['cleanup_error']=str(error)
            raise
        finally:
            write_json(output/'qualification.json',result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-request',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--ir',type=Path);parser.add_argument('--platform',type=Path)
    parser.add_argument('--constraints',type=Path);parser.add_argument('--out',type=Path)
    parser.add_argument('--expected-design');parser.add_argument('--minimum-instances',type=int,default=0)
    parser.add_argument('--source-commit')
    args=parser.parse_args()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,cancel)
    if args.worker_request:worker(args.worker_request);return
    if not args.ir or not args.platform or not args.out or not args.expected_design or args.minimum_instances<0:
        parser.error('--ir, --platform, --out and --expected-design are required; minimum instances must be nonnegative')
    result=qualify(args)
    print(json.dumps({'status':result['status'],'counts':result['arms']['native']['counts']}))


if __name__=='__main__':main()
