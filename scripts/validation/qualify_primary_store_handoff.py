#!/usr/bin/env python3
"""Offline native model/load differentials; no PATRON or feature claim.

Model scope uses round-robin or normal TritonPart initial ownership and retains
all model-reader constraints. Loads scope compares independent arithmetic on a
declared diagnostic vector; it is never a legal candidate or solver input.
Failures never relax constraints or switch implementations.
"""
from __future__ import annotations
from array import array
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
from emuflow.emuir_storage import is_emuir_sqlite
from emuflow.io import read_json, write_json
from emuflow.native_tools import native_file_generation, resolve_native_executable
from emuflow.partition import (active_partition_fpgas, build_clusters, normalize_partition_constraints,
                               _PackedClusterAssignment, CUT_MODE_STATIC_EXACT)
from emuflow.partition_ir import load_partition_ir, CompactPartitionIR
from emuflow.partition_pressure import build_partition_pressure_model
from emuflow.platform import Platform
from emuflow.routing import load_route_constraints
from emuflow.sta import sta_path_database_payload_path
from emuflow.tool_runtime import NativeToolProcess


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(4*1024*1024),b''): value.update(chunk)
    return value.hexdigest()


def cancel(signum,_frame):
    raise KeyboardInterrupt(f'primary handoff diagnostic cancelled by {signum}')


def worker(path):
    request=read_json(path)
    if request.get('schema')!='emuflow.primary-handoff-worker/v1':
        raise ValidationError('unknown primary handoff worker request')
    if (list(native_file_generation(Path(request['ir'])))!=request['ir_generation'] or
            not is_emuir_sqlite(Path(request['ir']))):
        raise ValidationError('primary handoff compact source generation changed')
    ir=load_partition_ir(Path(request['ir']))
    if not isinstance(ir,CompactPartitionIR):raise ValidationError('current compact EmuIR is required')
    if ir.value['design']['name']!=request['expected_design'] or len(ir.value['instances'])<request['minimum_instances']:
        raise ValidationError('primary handoff workload design/scale disagrees')
    platform=Platform.load(Path(request['platform']))
    raw=read_json(Path(request['constraints'])) if request['constraints'] else None
    constraints=normalize_partition_constraints(raw,ir,platform)
    clusters=build_clusters(ir,constraints,cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    if request.get('scope', 'model') == 'loads':
        # Arithmetic differential only: this vector is not a legal partition
        # candidate and is never submitted to a solver or feature validator.
        from emuflow.phase3_assignment_native import reduce_native_partition_loads
        from emuflow.resources import RESOURCE_FIELDS
        parts=tuple(sorted(fpga.id for fpga in active_partition_fpgas(platform,constraints)))
        records=clusters['clusters']
        assignment=_PackedClusterAssignment(parts,(i%len(parts) for i in range(len(records))))
        dimensions=('cells',*RESOURCE_FIELDS)
        started=time.monotonic()
        totals=dict.fromkeys(dimensions,0)
        loads={part:dict.fromkeys(dimensions,0) for part in parts}
        counts=dict.fromkeys(parts,0)
        for cluster in records:
            part=assignment[cluster['id']]
            counts[part]+=1
            values=(len(cluster['instances']),*(cluster['resources'].get(field,0) for field in RESOURCE_FIELDS))
            for field,value in zip(dimensions,values):
                totals[field]+=value;loads[part][field]+=value
        reference_seconds=time.monotonic()-started
        metrics={}
        started=time.monotonic()
        actual=reduce_native_partition_loads(records,assignment,parts,metrics=metrics)
        native_seconds=time.monotonic()-started
        expected=(totals,loads,counts)
        if actual!=expected:raise ValidationError('native primary load reduction differs from independent expanded records')
        payload=json.dumps(expected,sort_keys=True,separators=(',',':')).encode()
        write_json(Path(request['result']),{'status':'pass','scope':'Independent primary load arithmetic only; round-robin ownership is not a legal candidate, feature or whole-flow qualification.',
            'instances':len(ir.value['instances']),'clusters':len(records),
            'ordered_load_sha256':hashlib.sha256(payload).hexdigest(),
            'reference_wall_seconds':reference_seconds,'native_wall_seconds':native_seconds,
            'native_engine_metrics':metrics,'loads':loads,'cluster_counts':counts,'totals':totals},compact=True)
        return
    timing=read_json(Path(request['timing']))
    routes=load_route_constraints(Path(request['routes']) if request['routes'] else None,platform,frame_slots=1024)
    model=build_partition_pressure_model(ir,platform,clusters,constraints,timing,routes,
        timing_database_path=Path(request['timing']),independent_validation=False,native_store_model=True)
    if model.implementation_id!='native-primary-store-model-v2':
        raise ValidationError('diagnostic did not select direct primary ownership')
    parts=[fpga.id for fpga in model.partition_fpgas]
    if request['initialization']=='tritonpart':
        from emuflow.tritonpart import run_tritonpart
        initial=run_tritonpart(ir,platform,clusters,constraints,Path(request['scratch'])/'initializer',
            seed=0,executable=request['openroad'],timeout_seconds=21600,
            persist_input_manifest=False,defer_semantic_contract=True,_defer_derived_records=True)
        initial_mode='normal-generalized-tritonpart-seed0-initializer'
    else:
        indices=array('I',(i%len(parts) for i in range(len(model.clusters))))
        for cluster,fpga in model.clusters._fixed_by_cluster.items(): indices[cluster]=parts.index(fpga)
        initial={'cluster_assignment':_PackedClusterAssignment(parts,indices)}
        initial_mode='diagnostic-round-robin-with-declared-fixed-owners'
    results={}
    primary=model.primary_artifact
    native=resolve_native_executable('emuflow-native')
    root=Path(request['scratch'])
    for arm in ('snapshot','direct'):
        output=root/arm; output.mkdir()
        # Explicit offline v1 comparison only. Production always retains the
        # primary reference; no size heuristic or native failure selects v1.
        model.primary_artifact=None if arm=='snapshot' else primary
        started=time.monotonic()
        columns,board=model.write_requests(output,initial,0)
        emission_seconds=time.monotonic()-started
        command=[request['oracle'],'--signature',str(ir.path),str(model.timing_path)]
        if arm=='direct': command.append(str(primary))
        command.extend((str(columns),str(board)))
        with NativeToolProcess(native,command,environment=dict(os.environ),scratch_parent=output,
                               timeout_ms=21600000,maximum_bytes=65536) as process:
            completed=process.finish()
        if completed.returncode:
            raise ValidationError(f'{arm} model diagnostic failed: {completed.stdout}')
        fields=completed.stdout.split()
        if len(fields)!=7 or fields[0]!='MODEL_SIGNATURE' or len(fields[1])!=64 or any(c not in '0123456789abcdef' for c in fields[1]):
            raise ValidationError('model fingerprint envelope is invalid')
        results[arm]={'signature':fields[1],'model_counts':dict(zip(
            ('clusters','nets','paths','dimensions','capacity_domains'),map(int,fields[2:]))),
            'emitted_columns_bytes':columns.stat().st_size,'board_bytes':board.stat().st_size,
            'request_emission_wall_seconds':emission_seconds,'engine_metrics':completed.native_metrics}
    if (results['snapshot']['signature']!=results['direct']['signature'] or
            results['snapshot']['model_counts']!=results['direct']['model_counts']):
        raise ValidationError('direct primary model differs from v1 snapshot')
    result={'status':'pass','arms':results,'arm_order':['snapshot','direct'],'instances':len(ir.value['instances']),
            'primary_artifact_bytes':primary.stat().st_size,
            'initial_ownership':initial_mode,
            'scope':'Complete model-field differential only; no search, algorithm-selected cuts, whole-Phase3 performance or full-flow/physical qualification.'}
    write_json(Path(request['result']),result,compact=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-request',type=Path,help=argparse.SUPPRESS)
    for name in ('ir','timing-database','platform','oracle','out','constraints','route-constraints'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--scope',choices=('model','loads'),default='model',help='model boundary or independent raw load reduction')
    parser.add_argument('--expected-design')
    parser.add_argument('--source-commit',help='require matching versioned native/Python installation')
    parser.add_argument('--initialization',choices=('round-robin','tritonpart'),default='round-robin')
    parser.add_argument('--openroad',help='explicit normal initializer executable for tritonpart mode')
    parser.add_argument('--minimum-instances',type=int,default=1)
    args=parser.parse_args(argv)
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,cancel)
    if args.worker_request:
        worker(args.worker_request);return 0
    required=('ir','platform','out','expected_design')+ (('timing_database','oracle') if args.scope=='model' else ())
    for name in required:
        if getattr(args,name) is None:parser.error('--'+name.replace('_','-')+' is required')
    if args.minimum_instances<1:parser.error('--minimum-instances must be positive')
    if args.scope=='loads' and (args.initialization!='round-robin' or args.openroad is not None):
        parser.error('loads scope accepts only diagnostic round-robin ownership')
    root=args.out.resolve()
    if root.exists() and any(root.iterdir()):raise ValidationError('diagnostic output must be empty')
    root.mkdir(parents=True,exist_ok=True)
    scratch=root/'tmp';scratch.mkdir()
    try:
        if not is_emuir_sqlite(args.ir):raise ValidationError('current compact EmuIR is required')
        metadata=load_partition_ir(args.ir)
        if not isinstance(metadata,CompactPartitionIR):raise ValidationError('current compact EmuIR is required')
        if metadata.value['design']['name']!=args.expected_design or len(metadata.value['instances'])<args.minimum_instances:
            raise ValidationError('primary handoff workload design/scale disagrees')
        native=Path(resolve_native_executable('emuflow-native')).resolve()
        openroad=resolve_native_executable('openroad',args.openroad) if args.scope=='model' and args.initialization=='tritonpart' else None
        install=native.parent.parent
        if args.source_commit:
            if ((install/'SOURCE_COMMIT').read_text().strip()!=args.source_commit or
                    Path((install/'PYTHON_SOURCE_PATH').read_text().strip()).resolve()!=Path(__file__).resolve().parents[2]/'src'):
                raise ValidationError('versioned native/Python source identity disagrees')
        sources=[args.ir,args.platform,native,Path(__file__)]
        if args.scope=='model':
            timing=read_json(args.timing_database)
            sources.extend([args.timing_database,sta_path_database_payload_path(args.timing_database,timing),args.oracle])
        sources.extend(p for p in (args.constraints,args.route_constraints) if p is not None)
        if openroad is not None:sources.append(Path(openroad))
        identities=[{'path':str(p.resolve()),'generation':list(native_file_generation(p)),
                     'sha256':digest(p)} for p in sources]
        request={'schema':'emuflow.primary-handoff-worker/v1','ir':str(args.ir.resolve()),
            'ir_generation':list(native_file_generation(args.ir)),
            'scope':args.scope,'timing':str(args.timing_database.resolve()) if args.timing_database else None,'platform':str(args.platform.resolve()),
            'oracle':str(args.oracle.resolve()) if args.oracle else None,'constraints':str(args.constraints.resolve()) if args.constraints else None,
            'initialization':args.initialization,'openroad':openroad,
            'routes':str(args.route_constraints.resolve()) if args.route_constraints else None,
            'expected_design':args.expected_design,'minimum_instances':args.minimum_instances,
            'scratch':str(scratch),'result':str(scratch/'result.json')}
        write_json(scratch/'request.json',request,compact=True)
        env=dict(os.environ)
        for name in ('TMPDIR','TMP','TEMP','SQLITE_TMPDIR','XDG_CACHE_HOME'):env[name]=str(scratch)
        command=[sys.executable,str(Path(__file__).resolve()),'--worker-request',str(scratch/'request.json')]
        with NativeToolProcess(resolve_native_executable('emuflow-native'),command,environment=env,
                               scratch_parent=scratch,timeout_ms=21600000,termination_grace_ms=10000) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('primary handoff worker failed: '+completed.stdout)
        if (scratch/'result.json').stat().st_size>131072:raise ValidationError('diagnostic summary exceeds bound')
        result=read_json(scratch/'result.json')
        if result.get('status')!='pass':raise ValidationError('diagnostic worker did not pass')
        for identity in identities:
            if list(native_file_generation(Path(identity['path'])))!=identity['generation']:
                raise ValidationError('diagnostic source generation changed')
        result.update(schema='emuflow.primary-handoff-diagnostic/v1',sources=identities,source_commit=args.source_commit,
                      worker_metrics=completed.native_metrics)
        write_json(root/'qualification.json',result,compact=True)
    except BaseException as error:
        write_json(root/'failure.json',{'status':'fail','error':str(error)[:8192]},compact=True)
        raise
    finally:
        # The supervised worker and its model-reader children have exited;
        # no mapped circuit file is held by this parent, including on NFS.
        shutil.rmtree(scratch)
    return 0


if __name__=='__main__':raise SystemExit(main())
