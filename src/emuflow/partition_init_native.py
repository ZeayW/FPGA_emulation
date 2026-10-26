"""Native TritonPart initializer; the host carries configuration and references."""
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
from .native_tools import native_file_generation, require_native_schema, resolve_native_executable
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


def initialize(ir, platform, clusters_artifact, constraints, *, seed, executable,
               solution_input=None, weights_path=None, timeout_seconds=3600,
               seed_attempts=1, num_initial_solutions=50, num_best_initial_solutions=10,
               repair_min_used_fpgas=True, run_unweighted_baseline=False):
    from .partition import _PackedClusterAssignment, active_partition_fpgas, transported_cut_classes_for_clusters
    from .partition_limits import planning_resource_capacities
    from .resources import RESOURCE_FIELDS
    from .sta import partition_net_weights_payload_path
    from .sta_weights_native import _selector
    require_native_schema('partition-initialization-request-v1')
    records=clusters_artifact['clusters']
    if sys.byteorder!='little' or getattr(records,'native_artifact',None) is None:
        raise ValidationError('native initializer requires source-bound primary columns')
    source=StoreSource(Path(ir.path).resolve(),'emuir-compact-v2',records.native_source_generation)
    primary=StoreSource(Path(records.native_artifact).resolve(),'partition-cluster-columns-v2',records.native_generation)
    sources=[source,primary]
    if weights_path is not None:
        weights_path=Path(weights_path).resolve()
        value,selector=_selector(weights_path,'net-weight-selector-v2')
        payload=partition_net_weights_payload_path(weights_path,value)
        if value.get('schema')!='emuflow.partition-net-weights/v2' or payload is None:
            raise ValidationError('native initializer requires current typed net weights')
        sources.extend((selector,bind_store_source(payload,'net-weight-store-v1')))
    if solution_input is not None:sources.append(bind_store_source(Path(solution_input).resolve(),'tritonpart-solution-v1'))
    fpgas=active_partition_fpgas(platform,constraints);parts=tuple(fpga.id for fpga in fpgas)
    planning=planning_resource_capacities(fpgas,constraints.get('resource_reservations_by_fpga',{}))
    dimensions=('cells',*RESOURCE_FIELDS);balanced=constraints.get('balance_dimensions',())
    fixed=records._fixed_by_cluster
    if len(parts)>65536 or len(fixed)>65536:
        raise ValidationError('native initializer board/fixed configuration exceeds bound')
    require_store_sources_unchanged(sources)
    owner=_ClusterScratch(prefix='phase3-native-initialization-');root=Path(owner.name)
    try:
        request=root/'request.bin';selected=root/'selected';operation=root/'operation.json'
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QII',0x31544950464D5545,1,int(weights_path is not None)|2*int(solution_input is not None))
            for item in sources:writer.put('3Q4q',*item.generation)
            writer.catalog(parts);writer.catalog(sorted(transported_cut_classes_for_clusters(clusters_artifact)))
            writer.put('QQIIIIII',seed,timeout_seconds,seed_attempts,num_initial_solutions,num_best_initial_solutions,
                constraints['min_used_fpgas'],int(repair_min_used_fpgas),int(run_unweighted_baseline))
            writer.put('d',constraints['balance_tolerance'])
            writer.text(resolve_native_executable('openroad',executable) if solution_input is None else '')
            for field in dimensions:
                writer.put('Bd',int(balanced is None or field in balanced),constraints.get('balance_tolerance_by_dimension',{}).get(field,constraints['balance_tolerance']))
            for fpga in fpgas:
                for field in dimensions:writer.put('QQ',fpga.effective_capacity.get(field,0),planning[fpga.id].get(field,0))
            writer.put('I',len(fixed))
            for vertex,part in sorted(fixed.items()):writer.put('II',vertex,parts.index(part))
            writer.finish()
        finally:writer.close()
        require_store_sources_unchanged(sources)
        tool=resolve_native_executable('emuflow-native')
        command=[tool,'--report',str(operation),'phase3','initialize-tritonpart',str(source.path),str(primary.path),str(request),str(selected),*[str(item.path) for item in sources[2:]]]
        with NativeToolProcess(tool,command,environment=dict(os.environ),scratch_parent=root) as process:completed=process.finish()
        if completed.returncode:raise ValidationError('native partition initialization failed: '+completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size>256*1024:raise ValidationError('native initializer operation exceeds bound')
        proof=json.loads(operation.read_text());outputs=proof.get('outputs')
        if proof.get('stage')!='phase3.initialize-tritonpart' or proof.get('status')!='pass' or not isinstance(outputs,list) or len(outputs)!=2:
            raise ValidationError('native initializer producer result disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        originals=[]
        for record,path,schema in zip(outputs,(selected/'ownership.bin',selected/'summary.json'),('patron-initial-columns-v1','partition-initialization-summary-v1')):
            g=record.get('generation',{})
            if record.get('path')!=str(path.resolve()) or record.get('schema')!=schema or any(type(g.get(k)) is not int for k in fields):
                raise ValidationError('native initializer original output binding disagrees')
            originals.append(StoreSource(path.resolve(),schema,tuple(g[k] for k in fields)))
        require_store_sources_unchanged(originals)
        if originals[1].path.stat().st_size>4*1024*1024:raise ValidationError('native initializer summary exceeds bound')
        summary=json.loads(originals[1].path.read_text())
        if (summary.get('schema')!='emuflow.partition-initialization/v1' or summary.get('status')!='pass'
                or summary.get('implementation_id')!='native-tritonpart-initialization-v1' or summary.get('parts')!=list(parts)
                or summary.get('clusters')!=len(records) or summary.get('instances')!=len(records.source_cluster_indices)
                or summary.get('validation',{}).get('status')!='pass'):
            raise ValidationError('native initializer summary disagrees')
        with originals[0].path.open('rb') as file:data=mmap.mmap(file.fileno(),0,access=mmap.ACCESS_READ)
        mapping=_ColumnOwner(data,owner)
        try:
            count=len(records);expected=40+4*count+8*len(fixed)
            if len(data)!=expected or struct.unpack_from('<QIQQ',data)!=(0x315450494E415045,1,count,len(records.source_cluster_indices)):
                raise ValidationError('native initializer ownership envelope disagrees')
            crc=0
            for offset in range(0,len(data)-4,1024*1024):crc=zlib.crc32(data[offset:min(offset+1024*1024,len(data)-4)],crc)
            if crc!=struct.unpack_from('<I',data,len(data)-4)[0]:raise ValidationError('native initializer ownership checksum disagrees')
            if struct.unpack_from('<Q',data,28+4*count)[0]!=len(fixed):raise ValidationError('native initializer fixed coverage disagrees')
            assignment=object.__new__(_PackedClusterAssignment)
            assignment.part_ids=parts;assignment.part_indices=_MappedColumn(mapping,28,count,'I')
            assignment.native_initial_source=originals[0]
            assignment.native_initial_column=assignment.part_indices
            assignment.native_initial_parts=parts
            assignment.native_primary_source=primary
            assignment.native_emuir_source=source
            assignment.native_initial_fixed=dict(fixed)
            assignment.native_initial_fixed_parts=tuple(sorted(parts))
            require_store_sources_unchanged((*sources,*originals))
            summary['native_metrics']=completed.native_metrics
            return assignment,summary
        except BaseException:
            mapping.close();raise
    except BaseException:
        owner.cleanup();raise
