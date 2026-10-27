"""Bounded source facts and requests for current Phase 3 kernel qualification.

The native owners build clusters and exact semantics. This module contains no
SCC analysis, partition search, semantic graph construction, or publication
flow. Explicit fixture ownership is never repaired by an optimizer.
"""
from collections import Counter
from collections.abc import Mapping
import json
import mmap
import os
from pathlib import Path
import struct
import zlib

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from tests.native_column_fixture import _ClusterScratch, _ColumnOwner, _MappedColumn
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import resolve_native_executable
from emuflow.partition.partition import CLUSTERS_SCHEMA
from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.partition.partition import CUT_MODE_STATIC_EXACT
from emuflow.partition.partition import CUT_MODE_SEQUENTIAL_ONLY
from emuflow.partition.partition import DEFAULT_PARTITION_CUT_MODE
from emuflow.partition.partition import LEGAL_CUT_CLASSES
from emuflow.partition.partition import REPLICATED_NET_CLASSES
from emuflow.partition.partition import HARD_MACRO_RESOURCES
from tests.partition_views import InstanceOwners as _CompactInstanceAssignment
from tests.partition_views import ClusterOwners as _PackedClusterAssignment
from tests.partition_views import SelectedCuts as _StaticExactCutNetRecords
from tests.partition_balance_fixture import _cluster_assignment_summary
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


LIMIT = 65536


def materialize_facts(value):
    """Copy only a bounded fixture's logical records, never mmap ownership."""
    if isinstance(value,Mapping):
        return {key:materialize_facts(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        if len(value)>LIMIT:raise AssertionError('fixture record materialization exceeds bound')
        return [materialize_facts(item) for item in value]
    return value


class _FixtureAssignment(dict):
    def __deepcopy__(self,memo):
        result=materialize_facts(self);memo[id(self)]=result;return result


class _FixtureContract(dict):
    def __deepcopy__(self,memo):
        result=materialize_facts(self);memo[id(self)]=result;return result

from functools import wraps
from unittest.mock import patch


def _native_fixture(function):
    @wraps(function)
    def invoke(*args,**kwargs):
        if os.environ.get('EMUFLOW_NATIVE_ROOT'):
            return function(*args,**kwargs)
        from tests.native_build import emuflow_native
        with patch.dict(os.environ,{'EMUFLOW_NATIVE_ROOT':str(emuflow_native().parent.parent)}):
            return function(*args,**kwargs)
    return invoke



def compact_source(ir):
    if len(ir.value['instances']) > LIMIT:
        raise AssertionError('partition qualification fixture exceeds its explicit bound')
    if getattr(ir, 'path', None) is not None and str(ir.path).endswith(('.sqlite', '.sqlite3')):
        return ir
    from emuflow.ir.emuir_compact import write_compact_emuir
    from emuflow.partition.partition_ir import load_partition_ir
    scratch = _ClusterScratch(prefix='partition-source-facts-')
    path = Path(scratch.name)/'source.sqlite'
    write_compact_emuir(path, ir.to_dict())
    result = load_partition_ir(path)
    result._fixture_source_scratch = scratch
    return result


@_native_fixture
def normalize_partition_constraints(value, ir, platform, min_used_fpgas=None, balance_tolerance=None):
    from tests.native_partition_core_fixture import prepare_configuration
    return prepare_configuration(compact_source(ir).path, platform, value,
        min_used_fpgas=min_used_fpgas, balance_tolerance=balance_tolerance)


def load_partition_constraints(path, ir, platform, min_used_fpgas=None, balance_tolerance=None):
    return normalize_partition_constraints(read_json(path) if path else None, ir, platform,
        min_used_fpgas=min_used_fpgas, balance_tolerance=balance_tolerance)


@_native_fixture
def build_clusters(ir, constraints, cut_mode=DEFAULT_PARTITION_CUT_MODE,
        max_cross_fpga_dependency_depth=0, static_exact_candidate_policy='assignment-derived-acyclic-v2',
        *, _stream_eligible_policy_names=False, _native_compact_clusters=False):
    from tests.native_partition_core_fixture import build_native_candidate_ids
    from tests.native_partition_core_fixture import build_native_clusters
    if cut_mode not in {CUT_MODE_STATIC_EXACT, CUT_MODE_SEQUENTIAL_ONLY}:
        raise ValidationError('partition cut mode is invalid')
    if static_exact_candidate_policy != 'assignment-derived-acyclic-v2':
        raise ValidationError('unknown static exact candidate policy')
    if type(max_cross_fpga_dependency_depth) is not int or max_cross_fpga_dependency_depth < 0:
        raise ValidationError('max_cross_fpga_dependency_depth must be non-negative')
    source = compact_source(ir)
    eligible = build_native_candidate_ids(source) if cut_mode == CUT_MODE_STATIC_EXACT else set()
    rows = build_native_clusters(source, constraints, eligible)
    rows._fixture_ir = source
    if eligible:
        rows.candidate_implementation_id = eligible.implementation_id
        rows.candidate_native_metrics = eligible.native_metrics
    policy = dict(legal_cut_classes=sorted(LEGAL_CUT_CLASSES),
        replicated_net_classes=sorted(REPLICATED_NET_CLASSES),
        hard_macro_resources=sorted(HARD_MACRO_RESOURCES), hard_macro_granularity='instance')
    if cut_mode == CUT_MODE_STATIC_EXACT:
        policy.update(cut_mode=cut_mode, max_cross_fpga_dependency_depth=max_cross_fpga_dependency_depth,
            eligible_combinational_cut_nets=eligible.streaming_ordered_names(),
            transported_cut_classes=['combinational','register_input','register_output'],
            qualification='structural-partition-legality', candidate_selection_policy=static_exact_candidate_policy)
    return dict(schema=CLUSTERS_SCHEMA, design=ir.value['design']['name'], clusters=rows,
        instances=len(source.value['instances']), policy=policy)


def explicit_owner(ir, rows, owners, parts):
    """Encode the current ownership facts, including deliberately illegal facts."""
    from tests.native_partition_kernel_driver import _owner
    from tests.native_partition_core_fixture import ownership_sources
    if ownership_sources(rows, owners) is not None:
        return owners
    identities = [f'c{i:06d}' for i in range(len(rows))]
    if set(owners) != set(identities):
        raise ValidationError('cluster assignment exact coverage failed')
    try:
        indices = [parts.index(owners[identity]) for identity in identities]
    except ValueError as error:
        raise ValidationError('cluster assignment references unknown FPGA') from error
    if getattr(rows,'native_artifact',None) is None:
        raise ValidationError('native assignment primary ownership is missing')
    from emuflow.storage.store_publication import StoreSource
    require_store_sources_unchanged((StoreSource(Path(ir.path).resolve(),'emuir-compact-v2',rows.native_source_generation),
        StoreSource(Path(rows.native_artifact).resolve(),'partition-cluster-columns-v2',rows.native_generation)))
    scratch = _ClusterScratch(prefix='partition-owner-facts-')
    path = Path(scratch.name)/'ownership.bin'
    raw = struct.pack('<QIQQ',0x315450494E415045,1,len(rows),len(rows.source_cluster_indices))
    raw += struct.pack('<'+'I'*len(indices),*indices)+struct.pack('<Q',len(rows._fixed_by_cluster))
    raw += b''.join(struct.pack('<II',index,parts.index(part)) for index,part in sorted(rows._fixed_by_cluster.items()))
    path.write_bytes(raw+struct.pack('<I',zlib.crc32(raw)))
    value = _owner(path,rows,parts,bind_store_source(ir.path,'emuir-compact-v2'),
        bind_store_source(rows.native_artifact,'partition-cluster-columns-v2'),scratch)
    if isinstance(owners,_PackedClusterAssignment):
        owners.__dict__.update(value.__dict__); return owners
    return value


class _Leases:
    def __init__(self):
        self.scratch = _ClusterScratch(prefix='partition-semantics-facts-'); self.count = 1
    def lease(self):
        self.count += 1
        group = self
        class Lease:
            name = group.scratch.name
            live = True
            def cleanup(self):
                if self.live:
                    self.live = False; group.release()
            def __del__(self): self.cleanup()
        return Lease()
    def release(self):
        self.count -= 1
        if not self.count: self.scratch.cleanup()


@_native_fixture
def finalize_selected_semantics(ir, platform, records, assignment, instance_assignment,
        *, max_dependency_depth, candidate_selection_policy):
    """Call the existing source-bound owner; project its current typed outputs."""
    from tests.native_partition_core_fixture import _ownership_sources
    from emuflow.partition.phase3_policy import write_candidate_policy
    from tests.native_semantic_facts import read_nodes, read_captures, semantic_arrays
    from tests.native_assignment_publication_fixture import NativeAssignmentOwnership
    from emuflow.partition.combinational_cut import _canonical_sha256
    if candidate_selection_policy != 'assignment-derived-acyclic-v2':
        raise ValidationError('unknown static exact candidate policy')
    selected = _ownership_sources(ir,records,assignment)
    if selected is None: raise ValidationError('fixture requires typed ownership')
    kind,source,primary,owners = selected
    group = _Leases(); root = Path(group.scratch.name)
    try:
        policy,request,output = root/'policy.bin',root/'request.bin',root/'selected'
        operation=root/'producer.json'
        write_candidate_policy(policy,ir)
        refs = [source,primary,bind_store_source(policy,'candidate-policy-request-v1'),*owners]
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII',0x31514546464D5545,1,kind)
            for ref in refs: writer.put('3Q4q',*ref.generation)
            writer.catalog(assignment.part_ids);writer.put('Q',max_dependency_depth);writer.finish()
        finally: writer.close()
        tool = resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool,[tool,'--report',str(operation),'phase3','finalize-assignment-semantics',str(source.path),str(primary.path),
                str(policy),str(request),str(output),*(str(ref.path) for ref in owners)],
                environment=dict(os.environ),scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode: raise ValidationError('native final partition semantics failed: '+completed.stdout)
        require_store_sources_unchanged(refs)
        from emuflow.storage.store_publication import StoreSource
        if operation.stat().st_size>256*1024:raise ValidationError('native semantic operation exceeds bound')
        proof=json.loads(operation.read_text())
        if proof.get('stage')!='phase3.finalize-assignment-semantics' or proof.get('status')!='pass' or len(proof.get('outputs',()))!=4:
            raise ValidationError('native semantic output coverage disagrees')
        originals=[]
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        for record,name,schema in zip(proof['outputs'],('nodes.bin','captures.bin','counts.bin','summary.json'),
                ('exact-nodes-v2','exact-captures-v1','partition-cut-counts-v1','partition-finalization-summary-v1')):
            path=(output/name).resolve();generation=record.get('generation',{})
            if record.get('path')!=str(path) or record.get('schema')!=schema or any(type(generation.get(key)) is not int for key in fields):
                raise ValidationError('native semantic original output binding disagrees')
            originals.append(StoreSource(path,schema,tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(originals)
        if originals[3].path.stat().st_size>4*1024*1024:raise ValidationError('native semantic summary exceeds bound')
        summary=json.loads(originals[3].path.read_text());metrics=summary['metrics']
        nodes=read_nodes(originals[0].path,_validated_source=originals[0]);nodes.adopt_native_scratch(group.lease())
        captures=read_captures(originals[1].path,_validated_source=originals[1]);captures.adopt_native_scratch(group.lease())
        with originals[2].path.open('rb') as stream:data=mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
        count_owner=_ColumnOwner(data,group.lease())
        assert struct.unpack_from('<QIQ',data)==(0x31544350464D5545,1,len(nodes))
        assert len(data)==24+8*len(nodes) and zlib.crc32(data[:-4])==struct.unpack_from('<I',data,len(data)-4)[0]
        counts=_MappedColumn(count_owner,20,len(nodes),'Q');counts.native_source=originals[2]
        class Cuts(list):
            _sink_endpoints=counts
            @property
            def packed_payload_bytes(self): return 8*len(counts)
            def __len__(self):return len(nodes)
            def net_at(self,i):return nodes[i]['net']
            def __getitem__(self,i):return {key:nodes[i][key] for key in ('net','cut_class','source_fpgas','sink_fpgas')}|{'sink_endpoints':counts[i]}
        streams=semantic_arrays(nodes,captures,metrics,summary['launches'],require_source_proof=True)
        contract=_FixtureContract(schema='emuflow.static-exact-combinational-cut/v3',mode='static-exact-combinational',
            qualification='structural-partition-legality',max_cross_fpga_dependency_depth=max_dependency_depth,
            candidate_selection_policy=candidate_selection_policy,cut_nodes=nodes,**streams,capture_requirements=captures,
            metrics=metrics,source_identity=dict(emuir=dict(schema=ir.value['schema'],design=ir.value['design']['name'],
                instances=len(ir.value['instances']),nets=len(ir.value['nets']),qualification='phase1-index-plus-assignment-relation-v1'),
                canonical_platform_sha256=_canonical_sha256(platform.to_dict()),instance_assignment_sha256=summary['instance_assignment_sha256'],timing_source_sha256=None))
        require_store_sources_unchanged((*refs,*originals))
        nodes.native_finalization_metrics=completed.native_metrics
        nodes.native_finalization_implementation_id=summary['implementation_id']
        nodes.native_assignment_ownership=NativeAssignmentOwnership((source,primary,*owners),tuple(assignment.part_ids),assignment.part_indices)
        return _StaticExactCutNetRecords(Cuts(),nodes),summary['cut_metrics'],contract
    finally: group.release()


def compute_cut_nets(ir, owners):
    """Small independent endpoint ownership facts, without graph analysis."""
    if len(ir.value['instances']) > LIMIT: raise AssertionError('cut fixture exceeds bound')
    result=[]; primary=global_nets=0
    for net in ir.value['nets']:
        sources={owners[e['instance']] for e in net['drivers'] if e['instance'] is not None}
        sinks=Counter(owners[e['instance']] for e in net['sinks'] if e['instance'] is not None)
        if net['cut_class'] in {'clock','reset'}:global_nets+=int(len(sources|sinks.keys())>1);continue
        if not sources:
            primary+=int(net['cut_class']=='primary_input' and len(sinks)>1);continue
        remote=sorted(sinks.keys()-sources)
        if remote:result.append(dict(net=net['id'],cut_class=net['cut_class'],source_fpgas=sorted(sources),sink_fpgas=remote,sink_endpoints=sum(sinks[p] for p in remote)))
    result.sort(key=lambda r:r['net'])
    return result,dict(cut_nets=len(result),cut_sink_endpoints=sum(r['sink_endpoints'] for r in result),replicated_primary_inputs=primary,global_nets=global_nets)


@_native_fixture
def build_partition_assignment(ir, platform, clusters_artifact, constraints, cluster_assignment,
        provider, seed, provider_metadata=None, *, _include_semantic_contract=True,
        _stream_semantic_records=False,_defer_derived_records=False):
    from emuflow.partition.combinational_cut import semantic_contract_sha256
    from emuflow.partition.partition_limits import planning_resource_capacities
    rows=clusters_artifact['clusters'];source=getattr(rows,'_fixture_ir',None) or compact_source(ir)
    if getattr(rows,'implementation_id',None)=='native-primary-clusters-v1' and getattr(rows,'native_artifact',None) is None:
        raise ValidationError('native assignment primary ownership is missing')
    if getattr(rows,'native_artifact',None) is None:
        from tests.native_partition_core_fixture import build_native_clusters
        rows=build_native_clusters(source,constraints,set(clusters_artifact['policy'].get('eligible_combinational_cut_nets',())))
        rows._fixture_ir=source
    if _defer_derived_records and _include_semantic_contract:raise ValidationError('cannot defer semantic contract')
    owner=explicit_owner(source,rows,cluster_assignment,tuple(fpga.id for fpga in platform.fpgas))
    totals,loads,counts=_cluster_assignment_summary(rows,owner,tuple(fpga.id for fpga in platform.fpgas))
    instances=_CompactInstanceAssignment(source.path,rows,owner,_known_used_parts={part for part,count in counts.items() if count})
    policy=clusters_artifact['policy'];contract=None
    if _defer_derived_records:cuts=None;cut_metrics={}
    elif _include_semantic_contract and policy.get('cut_mode')==CUT_MODE_STATIC_EXACT:
        cuts,cut_metrics,contract=finalize_selected_semantics(source,platform,rows,owner,instances,
            max_dependency_depth=policy['max_cross_fpga_dependency_depth'],candidate_selection_policy=policy['candidate_selection_policy'])
    else:cuts,cut_metrics=compute_cut_nets(source,instances)
    planning=planning_resource_capacities(platform.fpgas,constraints.get('resource_reservations_by_fpga',{}))
    partitions=[]
    for fpga in platform.fpgas:
        resources={key:value for key,value in loads[fpga.id].items() if key!='cells' and value}
        if any(value>planning[fpga.id].get(key,0) for key,value in resources.items()):raise ValidationError('provider assignment exceeds reserved planning capacity')
        part=dict(fpga=fpga.id,cluster_count=counts[fpga.id],instance_count=loads[fpga.id]['cells'],resources=resources,
            effective_capacity=dict(sorted(fpga.effective_capacity.items())),utilization={key:resources.get(key,0)/value for key,value in sorted(fpga.effective_capacity.items()) if value>0})
        if not _stream_semantic_records:part['clusters']=[row['id'] for row in rows if owner[row['id']]==fpga.id]
        if constraints.get('resource_reservations_by_fpga'):
            part.update(planning_capacity=dict(sorted(planning[fpga.id].items())),resource_reservation=dict(sorted(constraints['resource_reservations_by_fpga'].get(fpga.id,{}).items())))
        partitions.append(part)
    result=_FixtureAssignment(schema=PARTITION_ASSIGNMENT_SCHEMA,design=ir.value['design']['name'],platform=platform.name,
        provider=provider,seed=seed,constraints=dict(constraints),cluster_assignment=owner,
        instance_assignment=instances,partitions=partitions,metrics=dict(instances=len(instances),clusters=len(rows),
            used_fpgas=sum(bool(p['instance_count']) for p in partitions),**cut_metrics))
    if cuts is None:result['_derived_records']='deferred-selection-only'
    else:result['cut_nets']=cuts
    if contract is not None:result.update(semantic_contract=contract,semantic_contract_sha256=semantic_contract_sha256(contract))
    if provider_metadata is not None:result['provider_metadata']=dict(provider_metadata)
    if not _stream_semantic_records:
        result['cluster_assignment']=dict(owner);result['instance_assignment']=dict(instances)
        if contract is not None:
            result['cut_nets']=materialize_facts(cuts)
            result['semantic_contract']=materialize_facts(contract)
    return result


def close_published_semantics(assignment):
    contract=assignment.get('semantic_contract') or {}
    nodes=contract.get('cut_nodes')
    if getattr(nodes,'native_finalization_implementation_id',None) is None:return
    nodes.close();contract['capture_requirements'].close();assignment['cut_nets']._cuts._sink_endpoints.owner.close()


@_native_fixture
def validate_partition_artifacts_online(platform, clusters_artifact, assignment_artifact, *, _native_audit=None):
    """Independent native decision gate plus bounded fixture envelope checks."""
    from tests.native_partition_core_fixture import audit_assignment
    from tests.native_partition_core_fixture import _OwnershipAudit
    from tests.partition_views import ClusterColumns as _CompactClusterRecords
    rows=clusters_artifact.get('clusters');owners=assignment_artifact.get('cluster_assignment');instances=assignment_artifact.get('instance_assignment')
    if clusters_artifact.get('schema')!=CLUSTERS_SCHEMA or assignment_artifact.get('schema')!=PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError('invalid online partition schema')
    if not isinstance(rows,list) or len(rows)>LIMIT or not isinstance(assignment_artifact.get('metrics'),dict):raise ValidationError('incomplete online partition artifact')
    constraints=assignment_artifact['constraints']
    if isinstance(instances,_CompactInstanceAssignment):
        if (instances._source_clusters is not rows.source_cluster_indices or instances._cluster_member_offsets is not rows._member_offsets):raise ValidationError('online compact cluster membership disagrees')
        if (instances._original_cluster_assignment is not owners or instances._original_cluster_parts is not instances._cluster_parts or instances._original_part_ids!=instances.part_ids):raise ValidationError('online original ownership/instance view disagrees with instances')
    if _native_audit is not None and not isinstance(_native_audit,_OwnershipAudit):raise ValidationError('online native audit is not source-bound evidence')
    certificate=_native_audit or (audit_assignment(platform,clusters_artifact,assignment_artifact) if isinstance(rows,_CompactClusterRecords) else None)
    if certificate is not None:
        facts=certificate.require(platform,assignment_artifact)
        from tests.native_partition_core_fixture import decode_partition_load_summary
        totals,loads,counts=decode_partition_load_summary(facts,rows,facts['parts'],tuple(fpga.id for fpga in platform.fpgas))
    else:
        expected={member:owners[row['id']] for row in rows for member in row['instances']}
        if dict(instances)!=expected:raise ValidationError('online cluster assignment disagrees with instances or exact coverage failed / spans FPGAs')
        totals,loads,counts=_cluster_assignment_summary(rows,owners,tuple(fpga.id for fpga in platform.fpgas))
        for fixed in constraints.get('fixed',[]):
            if expected.get(fixed['instance'])!=fixed['fpga']:raise ValidationError('online assignment violates a fixed instance')
        for group in constraints.get('groups',[]):
            if len({expected.get(member) for member in group['instances']})!=1:raise ValidationError('online assignment splits a fixed group')
    used={part for part,count in counts.items() if count}
    if len(used)<constraints['min_used_fpgas']:raise ValidationError('online assignment uses too few FPGAs')
    if not used<=set(constraints['active_fpgas']):raise ValidationError('online assignment uses an inactive FPGA')
    from tests.partition_balance_fixture import _validate_balance_loads
    from tests.partition_balance_fixture import _balance_partition_fpgas
    from emuflow.ir.resources import RESOURCE_FIELDS
    balance=_validate_balance_loads(_balance_partition_fpgas(platform,constraints.get('active_fpgas')),totals,loads,
        {key:bool(totals[key]) for key in RESOURCE_FIELDS},constraints['balance_tolerance'],constraints.get('balance_tolerance_by_dimension',{}),constraints.get('balance_dimensions'),constraints.get('resource_reservations_by_fpga',{}))
    result=dict(status='pass',instances=len(instances),clusters=len(rows),used_fpgas=len(used),illegal_cuts=0,
        **{key:value for key,value in assignment_artifact['metrics'].items() if key not in {'instances','clusters','used_fpgas'}},**balance,
        resources_by_fpga={part:{key:value for key,value in load.items() if key!='cells' and value} for part,load in loads.items()})
    if assignment_artifact.get('semantic_contract') is not None:result.update(cut_mode=CUT_MODE_STATIC_EXACT,qualification='structural-partition-legality',semantic_contract=dict(status='pass',**assignment_artifact['semantic_contract']['metrics']))
    return result


def validate_partition_artifacts(ir,platform,clusters,assignment):
    """Exercise current independent gates and compare explicit source facts."""
    from emuflow.partition.combinational_cut import semantic_contract_sha256
    checked=validate_partition_artifacts_online(platform,clusters,assignment)
    current=build_clusters(ir,assignment['constraints'],cut_mode=clusters['policy'].get('cut_mode',CUT_MODE_SEQUENTIAL_ONLY),
        max_cross_fpga_dependency_depth=clusters['policy'].get('max_cross_fpga_dependency_depth',0))
    if current['policy']!=clusters['policy'] or list(current['clusters'])!=list(clusters['clusters']):raise ValidationError('cluster reconstruction disagrees')
    expected=build_partition_assignment(ir,platform,current,assignment['constraints'],dict(assignment['cluster_assignment']),assignment['provider'],assignment['seed'])
    if list(expected['cut_nets'])!=list(assignment['cut_nets']):raise ValidationError('cut nets reconstruction disagrees')
    if semantic_contract_sha256(assignment.get('semantic_contract',{}))!=semantic_contract_sha256(expected.get('semantic_contract',{})):raise ValidationError('semantic_contract reconstruction disagrees')
    return checked


def semantic_contract_facts(ir,platform,instance_assignment,cut_nets,*,max_dependency_depth=0,
        candidate_selection_policy='assignment-derived-acyclic-v2',stream_records=False,cut_net_ordinals=None):
    """Explicit source owners -> current native semantic owner, for small tests."""
    from emuflow.platform.platform import Platform
    board=Platform.from_dict(platform) if isinstance(platform,dict) else platform
    constraints=normalize_partition_constraints({'schema':'emuflow.partition-constraints/v1',
        'min_used_fpgas':1,'balance_tolerance':1.0},ir,board)
    clusters=build_clusters(ir,constraints,max_cross_fpga_dependency_depth=max_dependency_depth,
        static_exact_candidate_policy=candidate_selection_policy)
    targets={}
    for row in clusters['clusters']:
        parts={instance_assignment[name] for name in row['instances']}
        if len(parts)!=1:raise ValidationError('fixture assignment splits a structural cluster')
        targets[row['id']]=parts.pop()
    value=build_partition_assignment(ir,board,clusters,constraints,targets,'fixture',0,_stream_semantic_records=True)
    expected={row['net']:(row['source_fpgas'],row['sink_fpgas']) for row in value['cut_nets']}
    if {row['net']:(row['source_fpgas'],row['sink_fpgas']) for row in cut_nets}!=expected:
        raise ValidationError('semantic cut source coverage disagrees')
    contract=value['semantic_contract']
    return contract if stream_records else materialize_facts(contract)


def candidate_index_facts(ir,**_options):
    from emuflow.ir.ir import EmuIR
    from tests.partition_graph_oracle import candidate_ids
    from tests.native_partition_core_fixture import build_native_candidate_ids
    return {'eligible_ids':candidate_ids(ir) if isinstance(ir,EmuIR) else build_native_candidate_ids(ir),'dependency_levels':{}}
