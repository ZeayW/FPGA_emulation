"""Bounded inputs and projections for retained native PATRON qualification.

The production stage owns modeling, search, decision and publication. These
functions only encode a small kernel request from its current board policy,
then lend the resulting columns to independent production audit consumers.
"""
from array import array
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace

from emuflow.common.errors import ValidationError
from tests.native_column_fixture import read_fixture_columns, _ClusterScratch, _ColumnOwner, _MappedColumn
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.storage.store_publication import StoreSource, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


def pressure_kernel_inputs(ir, platform, clusters, constraints, timing, routes, *, timing_database_path):
    from emuflow.partition.phase3_native import write_request
    records = clusters['clusters']
    if getattr(records, 'native_artifact', None) is None:
        raise ValidationError('native primary artifact reference is unavailable')
    if len(records) > 65536:
        raise ValidationError('PATRON kernel fixture exceeds cluster bound')
    with tempfile.TemporaryDirectory(prefix='patron-policy-facts-') as temporary:
        root = Path(temporary)
        for name, value in (('board', platform.to_dict()), ('constraints', constraints), ('routes', routes)):
            (root/(name+'.json')).write_text(json.dumps(value))
        solution = root/'solution'; solution.write_text('0\n')  # Configuration only; no initializer executes.
        config = json.loads(write_request(root/'config', ir.path, root/'board.json',
            constraints_path=root/'constraints.json', route_constraints_path=root/'routes.json',
            timing_database_path=timing_database_path, tritonpart_solution=solution).read_text())
    return SimpleNamespace(ir=ir, platform=platform, cluster_artifact=clusters, clusters=records,
        constraints=constraints, routes=config['route_constraints'], policy=config, domains=config['domains'],
        primary_artifact=Path(records.native_artifact), primary_generation=records.native_generation,
        timing_path=Path(config['inputs']['timing']['path']),
        generations=(records.native_source_generation, tuple(config['inputs']['timing']['generation'])),
        expected_paths=timing['payloads']['paths']['records'], normalization=timing['normalization'],
        partition_fpgas=sorted((fpga for fpga in platform.fpgas if fpga.id in constraints['active_fpgas']),key=lambda fpga:fpga.id))


def write_pressure_requests(value, root, initial, max_moves):
    from emuflow.partition.partition_limits import planning_resource_capacities
    from tests.native_partition_core_fixture import _ownership_sources
    from emuflow.ir.resources import RESOURCE_FIELDS
    root = Path(root)
    if value.primary_artifact is None:
        raise ValidationError('selected direct primary ownership is unavailable')
    sources = [StoreSource(Path(value.ir.path), 'emuir-compact-v2', value.generations[0]),
        StoreSource(value.timing_path, 'timing-path-store-v1', value.generations[1]),
        StoreSource(value.primary_artifact, 'partition-cluster-columns-v2', value.primary_generation)]
    require_store_sources_unchanged(sources)
    parts = [fpga.id for fpga in value.partition_fpgas]
    current = initial['cluster_assignment']
    columns, board = root/'initial.bin', root/'board.bin'
    if getattr(current, 'native_initial_source', None) is not None:
        owned = _ownership_sources(value.ir, value.clusters, current)
        columns, original_parts = owned[3][0].path, current.part_ids
    else:
        original_parts = getattr(current, 'part_ids', tuple(parts))
        writer = CheckedRequestWriter(columns)
        try:
            writer.put('QIQQ', 0x315450494E415045, 1, len(value.clusters), len(value.clusters.source_cluster_indices))
            writer.bytes(array('I', (original_parts.index(current[f'c{i:06d}']) for i in range(len(value.clusters)))))
            writer.put('Q', len(value.clusters._fixed_by_cluster))
            for cluster, fpga in value.clusters._fixed_by_cluster.items(): writer.put('II', cluster, parts.index(fpga))
            writer.finish()
        finally: writer.close()
    writer = CheckedRequestWriter(board)
    try:
        writer.put('QI', 0x3151455254415045, 2)
        for source in sources: writer.put('3Q4q', *source.generation)
        writer.put('3Q4q', *native_file_generation(columns))
        writer.catalog(parts); writer.catalog(original_parts)
        writer.catalog(value.policy['cluster_policy']['transported_cut_classes'])
        writer.put('Q', value.expected_paths)
        routes = value.routes
        # Fixed current v14 policy facts, independently checked against model/decision outputs.
        writer.put('10i', -1 if routes['max_route_hops'] is None else routes['max_route_hops'],
            routes['tdm_max_ratio'], routes['tdm_ratio_quantum'], value.constraints['min_used_fpgas'],
            max_moves, 4, 2048, 32, 64, routes.get('tdm_min_ratio',1))
        writer.put('5d', *(value.normalization[key] for key in ('positive_slack_scale_ns','negative_slack_scale_ns','max_clock_period_ns')),0.0,0.0)
        writer.put('6i',4,0,8,512,256,256)
        dimensions = ('cells', *RESOURCE_FIELDS)
        balanced = value.constraints.get('balance_dimensions', ())
        for field in dimensions:
            writer.put('Bd', int(balanced is None or field in balanced),
                value.constraints.get('balance_tolerance_by_dimension',{}).get(field,value.constraints['balance_tolerance']))
        planning = planning_resource_capacities(value.partition_fpgas,value.constraints.get('resource_reservations_by_fpga',{}))
        for fpga in value.partition_fpgas:
            for field in dimensions: writer.put('QQ',fpga.effective_capacity.get(field,0),planning[fpga.id].get(field,0))
        writer.put('I',len(value.domains))
        for domain in value.domains:
            writer.put('iBdiiiq',domain['lanes'],int(domain['sll']),domain['slope'],domain['opposite'],
                domain['bank_count'],domain['wires_per_bank'],domain['capacity_bits'])
        for source in parts:
            for sink in parts:
                path = value.policy['shortest_routes'][source][sink]
                writer.put('i',-1 if path is None else len(path))
                for domain, delay in path or (): writer.put('Id',domain,delay)
        writer.finish()
    finally: writer.close()
    require_store_sources_unchanged(sources)
    return columns, board


def run_pressure_kernel(value, initial, *, max_moves=0):
    from tests.partition_views import ClusterOwners as _PackedClusterAssignment
    from tests.native_partition_facts import (build_partition_assignment)
    count = len(value.clusters)
    workspace = _ClusterScratch(prefix='patron-kernel-facts-'); root = Path(workspace.name)
    retained = False
    try:
        columns, board = write_pressure_requests(value,root,initial,max_moves)
        output, proof = root/'result.bin', root/'producer.json'
        tool = resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool,[tool,'--report',str(proof),'phase3','refine-patron-from-primary',
            str(value.ir.path),str(value.timing_path),str(value.primary_artifact),str(columns),str(board),str(output)],
            environment={key:item for key,item in os.environ.items() if not key.startswith('EMUFLOW_PATRON_')},scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode: raise ValidationError('native PATRON kernel failed: '+completed.stdout)
        operation = json.loads(proof.read_text())
        fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        sources = tuple(StoreSource(Path(row['path']),row['schema'],tuple(row['generation'][key] for key in fields)) for row in operation['outputs'])
        require_store_sources_unchanged(sources)
        data = read_fixture_columns(output)
        # Trusted kernel output is projected here; malformed data is tested via native audit-decision.
        offset = 20+struct.unpack_from('<I',data,16)[0]
        before, after = struct.unpack_from('<8d',data,offset), struct.unpack_from('<8d',data,offset+64)
        capacity = struct.unpack_from('<4q',data,offset+128); hops = struct.unpack_from('<4q',data,offset+160)
        moves,batches,changes = struct.unpack_from('<3Q',data,offset+192)
        owner = object.__new__(_PackedClusterAssignment); owner.part_ids = tuple(fpga.id for fpga in value.partition_fpgas)
        mapping = _ColumnOwner(data,workspace)
        owner.part_indices = _MappedColumn(mapping,len(data)-4-4*count,count,'I')
        owner.native_output_column, owner.native_output_parts = owner.part_indices,owner.part_ids
        owner.native_output_source,owner.native_order_source = sources[:2]
        owner.native_emuir_source = StoreSource(Path(value.ir.path).resolve(),'emuir-compact-v2',value.generations[0])
        owner.native_primary_source = StoreSource(value.primary_artifact.resolve(),'partition-cluster-columns-v2',value.primary_generation)
        retained = True
        final = build_partition_assignment(value.ir,value.platform,value.cluster_artifact,value.constraints,owner,
            'patron-predictive-timing-flow-native-v14',initial.get('seed',0),_include_semantic_contract=False,
            _stream_semantic_records=True,_defer_derived_records=True)
        def metrics(raw, selected):
            result = dict(zip(('negative_paths','maximum_predicted_tdm_ratio','maximum_capacity_domain_load',
                'total_path_partition_transitions','total_bit_hops','cut_bits'),map(int,raw[2:])))
            result.update(worst_normalized_slack=-raw[0],total_negative_normalized_slack=-raw[1],objective_key=list(raw),
                transport_capacity_violations=capacity[2*selected],transport_capacity_excess_bits=capacity[2*selected+1],
                maximum_path_hops=hops[2*selected],maximum_path_tdm_ratio_sum=hops[2*selected+1])
            return result
        summary = json.loads(Path(str(output)+'.summary.json').read_text())
        trace = dict(mode='endpoint-exact-critical-flow-v14', initial_metrics=metrics(before,0),final_metrics=metrics(after,1),
            move_count=moves,batch_count=batches,change_count=changes,native_input_transport='native-primary-store-model-v2',
            model_counts={key:summary[key] for key in ('clusters','nets','paths','capacity_domains')},
            native_model_metrics={key:summary[stage]['metrics'] for key,stage in (('model_build','model_build'),('search','search'))})
        for path in (board,root/'initial.bin'): path.unlink(missing_ok=True)
        return final,trace
    finally:
        if not retained:workspace.cleanup()
