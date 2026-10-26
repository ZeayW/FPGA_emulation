"""Selected native ownership to final Static Exact records; bounded host only."""
import json
import mmap
import os
from pathlib import Path
import struct
import sys
import zlib

from .errors import ValidationError
from .native_columns import _ClusterScratch, _ColumnOwner, _MappedColumn
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


class _ScratchGroup:
    """A directory retires only after every independently mapped file closes."""
    def __init__(self):
        self.scratch=_ClusterScratch(prefix='phase3-final-semantics-')
        self.borrowers=1

    def lease(self):
        group=self
        self.borrowers+=1
        class Lease:
            live=True
            def cleanup(self):
                if self.live:
                    self.live=False
                    group.release()
            def __del__(self):self.cleanup()
        return Lease()

    def release(self):
        self.borrowers-=1
        if not self.borrowers:self.scratch.cleanup()


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


class _NodeTopology:
    def __init__(self,nodes):self.nodes=nodes
    def __len__(self):return len(self.nodes)
    def dependency_indices(self,index):return self.nodes.native_dependency_indices(index)
    def dependency_count(self,index):return len(self.dependency_indices(index))


def close_published_semantics(assignment):
    """The Phase 3 command returns a report; published stores own downstream data."""
    contract=assignment.get('semantic_contract') or {}
    nodes=contract.get('cut_nodes')
    if getattr(nodes,'native_finalization_implementation_id',None) is None:return
    captures=contract['capture_requirements']
    counts=assignment['cut_nets']._cuts._sink_endpoints
    nodes.close();captures.close();counts.owner.close()


def finalize_selected_semantics(ir,platform,records,assignment,instance_assignment,*,max_dependency_depth,candidate_selection_policy):
    """Return None only for explicit reference owners, never after native failure."""
    from .partition import _CompactCutNetRecords, _StaticExactCutNetRecords
    from .phase3_candidates_native import write_candidate_policy
    from .phase3_exact_records import (read_native_static_exact_cut_node_stream,
        read_native_static_exact_capture_stream,build_static_exact_record_streams,_NativeNodeLaunchFlags)
    from .combinational_cut import (STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
        STATIC_EXACT_CANDIDATE_POLICIES,_bounded_emuir_identity,_canonical_sha256)
    selected=_ownership_sources(ir,records,assignment)
    if selected is None:return None
    if (sys.byteorder!='little' or type(max_dependency_depth) is not int or max_dependency_depth<0
            or candidate_selection_policy not in STATIC_EXACT_CANDIDATE_POLICIES):
        raise ValidationError('invalid native final semantic policy')
    require_native_schema('partition-finalization-request-v1')
    kind,source,primary,owners=selected
    group=_ScratchGroup();root=Path(group.scratch.name)
    nodes=captures=count_owner=None
    try:
        policy=root/'policy.bin';request=root/'request.bin';output=root/'selected';operation=root/'producer.json'
        if write_candidate_policy(policy,ir)!=source.generation:
            raise ValidationError('native final semantic source generation changed')
        sources=[source,primary,bind_store_source(policy,'candidate-policy-request-v1'),*owners]
        require_store_sources_unchanged(sources)
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QII',0x31514546464D5545,1,kind)
            for item in sources:writer.put('3Q4q',*item.generation)
            writer.catalog(assignment.part_ids);writer.put('Q',max_dependency_depth);writer.finish()
        finally:writer.close()
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(operation),'phase3','finalize-assignment-semantics',
            str(source.path),str(primary.path),str(policy),str(request),str(output),*[str(item.path) for item in owners]]
        with NativeToolProcess(executable,command,environment=dict(os.environ),timeout_ms=21600000,scratch_parent=root) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native final partition semantics failed: '+completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size>256*1024:raise ValidationError('native semantic operation exceeds bound')
        proof=json.loads(operation.read_text())
        if (proof.get('stage')!='phase3.finalize-assignment-semantics' or proof.get('status')!='pass'
                or not isinstance(proof.get('outputs'),list) or len(proof['outputs'])!=4):
            raise ValidationError('native semantic producer result disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        originals=[]
        for record,name,schema in zip(proof['outputs'],('nodes.bin','captures.bin','counts.bin','summary.json'),
                ('exact-nodes-v2','exact-captures-v1','partition-cut-counts-v1','partition-finalization-summary-v1')):
            path=(output/name).resolve();generation=record.get('generation',{})
            if (record.get('path')!=str(path) or record.get('schema')!=schema
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ValidationError('native semantic original output binding disagrees')
            originals.append(StoreSource(path,schema,tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(originals)
        if originals[3].path.stat().st_size>4*1024*1024:raise ValidationError('native semantic summary exceeds bound')
        summary=json.loads(originals[3].path.read_text());metrics=summary.get('metrics',{})
        if (summary.get('schema')!='emuflow.partition-final-semantics/v1' or summary.get('status')!='pass'
                or summary.get('implementation_id')!='native-selected-partition-semantics-v1'
                or summary.get('clusters')!=len(records) or summary.get('instances')!=len(ir.value['instances'])
                or summary.get('parts')!=list(instance_assignment.part_ids)
                or summary.get('validation',{}).get('status')!='pass'
                or not isinstance(metrics,dict) or any(type(value) is not int or value<0 for value in metrics.values())
                or set(metrics)!={'transported_cut_nets','combinational_cut_nets','dependency_edges','maximum_dependency_level',
                    'maximum_combinational_dependency_depth','logic_segments','capture_requirements'}):
            raise ValidationError('native semantic summary disagrees')
        digest=summary.get('instance_assignment_sha256')
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValidationError('native semantic assignment identity disagrees')
        nodes=read_native_static_exact_cut_node_stream(originals[0].path,_validated_source=originals[0])
        nodes.adopt_native_scratch(group.lease())
        captures=read_native_static_exact_capture_stream(originals[1].path,_validated_source=originals[1])
        captures.adopt_native_scratch(group.lease())
        if len(nodes)!=metrics['transported_cut_nets'] or len(captures)!=metrics['capture_requirements']:
            raise ValidationError('native semantic record/summary coverage disagrees')
        with originals[2].path.open('rb') as stream:data=mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
        count_owner=_ColumnOwner(data,group.lease())
        if (len(data)!=24+8*len(nodes) or struct.unpack_from('<QIQ',data)!=(0x31544350464D5545,1,len(nodes))):
            raise ValidationError('native semantic count envelope disagrees')
        crc=0
        for offset in range(0,len(data)-4,1024*1024):crc=zlib.crc32(data[offset:min(offset+1024*1024,len(data)-4)],crc)
        if crc!=struct.unpack_from('<I',data,len(data)-4)[0]:raise ValidationError('native semantic count checksum disagrees')
        counts=_MappedColumn(count_owner,20,len(nodes),'Q');counts.native_source=originals[2]
        part_ids=tuple(instance_assignment.part_ids)
        class CutRecords(_CompactCutNetRecords):
            def __init__(self):
                list.__init__(self);self._sink_endpoints=counts;self._canonical=True
                self._part_ids=part_ids
            def __len__(self):return len(nodes)
            def net_at(self,index):return nodes.binary_node(index)[0].decode('utf-8')
            def _record(self,index):
                row=nodes[index]
                return {key:row[key] for key in ('net','cut_class','source_fpgas','sink_fpgas')}|{'sink_endpoints':counts[index]}
            @property
            def packed_payload_bytes(self):return counts.itemsize*len(counts)
        flags=_NativeNodeLaunchFlags(nodes,metrics,summary['launches'])
        streams=build_static_exact_record_streams(nodes,captures,flags,integer_topology=_NodeTopology(nodes))
        contract={'schema':STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,'mode':'static-exact-combinational',
            'qualification':'structural-partition-legality','max_cross_fpga_dependency_depth':max_dependency_depth,
            'candidate_selection_policy':candidate_selection_policy,'cut_nodes':nodes,**streams,'capture_requirements':captures,
            'metrics':metrics,'source_identity':{'emuir':_bounded_emuir_identity(ir),
                'canonical_platform_sha256':_canonical_sha256(platform.to_dict()),'instance_assignment_sha256':digest,'timing_source_sha256':None}}
        nodes.native_finalization_metrics=proof['metrics']
        nodes.native_finalization_implementation_id=summary['implementation_id']
        from .phase3_assignment_native import NativeAssignmentOwnership
        nodes.native_assignment_ownership=NativeAssignmentOwnership(
            (source,primary,*owners),tuple(assignment.part_ids),assignment.part_indices)
        require_store_sources_unchanged((*sources,*originals))
        return _StaticExactCutNetRecords(CutRecords(),nodes),summary['cut_metrics'],contract
    except BaseException:
        if nodes is not None:nodes.close()
        if captures is not None:captures.close()
        if count_owner is not None:count_owner.close()
        raise
    finally:
        group.release()
