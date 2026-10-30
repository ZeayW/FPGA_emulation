"""Bounded protocol drivers for independent native kernel qualification only.

These fixtures encode requests for existing kernels and project their output
columns. They contain no partition/search policy or alternate stage flow.
"""
import json
import math
import os
from pathlib import Path
import struct
import tempfile

from emuflow.common.errors import ValidationError
from tests.native_column_fixture import read_fixture_columns, _ClusterScratch, _ColumnOwner, _MappedColumn
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import resolve_native_executable
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


def _invoke(stage, root, sources, request, output, *, executable=None):
    require_store_sources_unchanged(sources)
    tool = resolve_native_executable('emuflow-native', executable)
    proof = root/'operation.json'
    command = [tool, '--report', str(proof), 'phase3', stage,
               str(sources[0].path), str(sources[1].path), str(request), str(output),
               *(str(source.path) for source in sources[2:])]
    with NativeToolProcess(tool, command, environment=dict(os.environ), scratch_parent=root,
                           timeout_ms=21600000) as process:
        completed = process.finish()
    if completed.returncode:
        raise ValidationError('native qualification kernel failed: '+completed.stdout)
    require_store_sources_unchanged(sources)
    return json.loads(proof.read_text()), completed


def _owner(path, records, parts, source, primary, scratch):
    from tests.partition_views import ClusterOwners as _PackedClusterAssignment
    if len(records) > 65536:
        raise ValidationError('kernel fixture exceeds cluster bound')
    data = read_fixture_columns(path)
    mapping = _ColumnOwner(data, scratch)
    assert struct.unpack_from('<QIQQ', data) == (0x315450494E415045, 1, len(records), len(records.source_cluster_indices))
    value = object.__new__(_PackedClusterAssignment)
    value.part_ids = tuple(parts)
    value.part_indices = _MappedColumn(mapping, 28, len(records), 'I')
    value.native_initial_source = bind_store_source(path, 'patron-initial-columns-v1')
    value.native_initial_column = value.part_indices
    value.native_initial_parts = tuple(parts)
    value.native_primary_source, value.native_emuir_source = primary, source
    value.native_initial_fixed = dict(records._fixed_by_cluster)
    value.native_initial_fixed_parts = tuple(sorted(parts))
    return value


def _weights(sources, path):
    if path is None:
        return
    from emuflow_tools.timing.sta import partition_net_weights_payload_path
    from emuflow_tools.timing.sta_weights_native import _selector
    selector, source = _selector(Path(path).resolve(), 'net-weight-selector-v2')
    payload = partition_net_weights_payload_path(Path(path).resolve(), selector)
    if selector.get('schema') != 'emuflow.partition-net-weights/v2' or payload is None:
        raise ValidationError('kernel fixture requires current typed weights')
    sources.extend((source, bind_store_source(payload, 'net-weight-store-v1')))


def initialize(ir, platform, clusters_artifact, constraints, *, seed, executable,
               solution_input=None, weights_path=None, timeout_seconds=3600,
               seed_attempts=1, num_initial_solutions=50, num_best_initial_solutions=10,
               repair_min_used_fpgas=True, run_unweighted_baseline=False):
    from tests.partition_balance_fixture import active_partition_fpgas
    from tests.partition_balance_fixture import transported_cut_classes_for_clusters
    from emuflow_tools.partition.partition_limits import planning_resource_capacities
    from emuflow.ir.resources import RESOURCE_FIELDS
    records = clusters_artifact['clusters']
    if len(records) > 65536:
        raise ValidationError('kernel fixture exceeds cluster bound')
    source = StoreSource(Path(ir.path).resolve(), 'emuir-compact-v2', records.native_source_generation)
    primary = StoreSource(Path(records.native_artifact).resolve(), 'partition-cluster-columns-v2', records.native_generation)
    sources = [source, primary]
    _weights(sources, weights_path)
    if solution_input is not None:
        sources.append(bind_store_source(Path(solution_input).resolve(), 'tritonpart-solution-v1'))
    fpgas = active_partition_fpgas(platform, constraints)
    parts = tuple(fpga.id for fpga in fpgas)
    planning = planning_resource_capacities(fpgas, constraints.get('resource_reservations_by_fpga', {}))
    dimensions, balanced = ('cells', *RESOURCE_FIELDS), constraints.get('balance_dimensions', ())
    fixed = records._fixed_by_cluster
    scratch = _ClusterScratch(prefix='kernel-initializer-fixture-')
    root = Path(scratch.name)
    try:
        request, selected = root/'request.bin', root/'selected'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31544950464D5545, 1, int(weights_path is not None)|2*int(solution_input is not None))
            for item in sources: writer.put('3Q4q', *item.generation)
            writer.catalog(parts); writer.catalog(sorted(transported_cut_classes_for_clusters(clusters_artifact)))
            writer.put('QQIIIIII', seed, timeout_seconds, seed_attempts, num_initial_solutions, num_best_initial_solutions,
                       constraints['min_used_fpgas'], int(repair_min_used_fpgas), int(run_unweighted_baseline))
            writer.put('d', constraints['balance_tolerance'])
            writer.text(resolve_native_executable('openroad', executable) if solution_input is None else '')
            for field in dimensions:
                writer.put('Bd', int(balanced is None or field in balanced), constraints.get('balance_tolerance_by_dimension', {}).get(field, constraints['balance_tolerance']))
            for fpga in fpgas:
                for field in dimensions: writer.put('QQ', fpga.effective_capacity.get(field, 0), planning[fpga.id].get(field, 0))
            writer.put('I', len(fixed))
            for vertex, part in sorted(fixed.items()): writer.put('II', vertex, parts.index(part))
            writer.finish()
        finally: writer.close()
        _, completed = _invoke('initialize-tritonpart', root, sources, request, selected)
        summary = json.loads((selected/'summary.json').read_text())
        summary['native_metrics'] = completed.native_metrics
        return _owner(selected/'ownership.bin', records, parts, source, primary, scratch), summary
    except BaseException:
        scratch.cleanup()
        raise


def run_hops(ir, platform, clusters, constraints, assignment, routes, *, weights_path=None,
             executable=None, audit_only=False):
    from tests.native_partition_core_fixture import _ownership_sources
    from tests.partition_balance_fixture import active_partition_fpgas
    from tests.partition_balance_fixture import transported_cut_classes_for_clusters
    from tests.partition_balance_fixture import validate_cluster_assignment_balance
    from emuflow_tools.partition.partition_limits import planning_resource_capacities
    from emuflow_tools.partition.partition_topology import _shortest_hop_distances
    records = clusters['clusters']
    if len(records) > 65536:
        raise ValidationError('kernel fixture exceeds cluster bound')
    selected = _ownership_sources(ir, records, assignment['cluster_assignment'])
    if selected is None:
        raise ValidationError('hop kernel fixture requires source-bound primary ownership')
    kind, source, primary, owners = selected
    sources = [source, primary, *owners]
    _weights(sources, weights_path)
    fpgas = active_partition_fpgas(platform, constraints)
    parts = tuple(fpga.id for fpga in fpgas)
    balance = validate_cluster_assignment_balance(platform, records, assignment['cluster_assignment'], constraints['balance_tolerance'],
        constraints.get('balance_tolerance_by_dimension', {}), constraints.get('active_fpgas'), constraints.get('balance_dimensions', ()),
        constraints.get('resource_reservations_by_fpga', {}))
    planning = planning_resource_capacities(fpgas, constraints.get('resource_reservations_by_fpga', {}))
    dimensions = balance['balance_dimensions']
    distances = _shortest_hop_distances(platform, routes)
    fixed = records._fixed_by_cluster
    scratch = _ClusterScratch(prefix='kernel-hops-fixture-')
    root = Path(scratch.name)
    try:
        request, output = root/'request.bin', root/'selected'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31514850464D5545, 1, kind|4*int(weights_path is not None)|8*int(audit_only))
            for item in sources: writer.put('3Q4q', *item.generation)
            writer.catalog(parts); writer.catalog(assignment['cluster_assignment'].part_ids)
            writer.catalog(sorted(transported_cut_classes_for_clusters(clusters))); writer.catalog(dimensions)
            writer.put('QQ', routes['max_route_hops'], constraints['min_used_fpgas'])
            for a in parts:
                for b in parts: writer.put('i', -1 if distances[a][b] is None else distances[a][b])
            for fpga in fpgas:
                for dimension in dimensions:
                    allowed = balance['balance_allowed_loads'][fpga.id][dimension]
                    writer.put('dQ', allowed, math.ceil(allowed) if dimension == 'cells' else planning[fpga.id][dimension])
            writer.put('I', len(fixed))
            for vertex, fpga in sorted(fixed.items()): writer.put('II', vertex, parts.index(fpga))
            writer.finish()
        finally: writer.close()
        stage = 'audit-hops-from-primary' if audit_only else 'refine-hops-from-primary'
        proof, _ = _invoke(stage, root, sources, request, output, executable=executable)
        summary = json.loads((output/'summary.json').read_text())
        summary['native_operation_metrics'] = proof['metrics']
        if audit_only:
            scratch.cleanup()
            return None, summary
        return _owner(output/'ownership.bin', records, parts, source, primary, scratch), summary
    except BaseException:
        scratch.cleanup()
        raise


def publish_partition(output, ir, clusters, assignment, report):
    from tests.native_assignment_publication_fixture import native_assignment_cut_source
    from tests.native_cluster_catalog_fixture import catalog_sources, write_catalog_request
    cut = native_assignment_cut_source(assignment)
    records, policy = clusters['clusters'], clusters['policy']
    eligible = policy.get('eligible_combinational_cut_nets', [])
    catalog = catalog_sources(records, eligible)
    contract = assignment['semantic_contract']
    header = {key:value for key,value in contract.items() if key not in {'cut_nodes','dependency_edges','logic_segments','capture_requirements'}}
    config = {'index_metadata':{'schema':'emuflow.phase3-assignment-sqlite/v4','design':assignment['design'],
        'platform':assignment['platform'],'provider':assignment['provider'],'seed':assignment['seed'],
        'source_instance_count':len(assignment['instance_assignment']),'constraints':assignment['constraints'],
        'semantic_contract_sha256':assignment['semantic_contract_sha256'],'semantic_contract_schema':contract['schema'],
        'semantic_contract_header':header,'semantic_contract_binary':None},
        'document':{key:value for key,value in assignment.items() if key not in {'cluster_assignment','instance_assignment','partitions','semantic_contract','cut_nets'}},
        'partitions':[{key:value for key,value in part.items() if key!='clusters'} for part in assignment['partitions']], 'report':report}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.partition-kernel-fixture-', dir=output.parent) as temporary:
        root = Path(temporary); catalog_request, request = root/'catalog.bin', root/'request.bin'
        document = {key:value for key,value in clusters.items() if key not in {'clusters','policy'}}
        document['policy'] = {key:value for key,value in policy.items() if key!='eligible_combinational_cut_nets'}
        write_catalog_request(catalog_request, clusters, document, eligible, 'eligible_combinational_cut_nets' in policy, catalog)
        sources = (catalog[0], catalog[1], bind_store_source(cut.paths[0], 'exact-nodes-v2'),
                   bind_store_source(cut.paths[1], 'exact-captures-v1'), cut.auxiliary_sources[0],
                   bind_store_source(catalog_request, 'cluster-catalog-request-v1'), catalog[2], *cut.ownership.sources[2:])
        if tuple(item.generation for item in sources[2:4]) != cut.generations:
            raise ValidationError('publication kernel semantic generation changed')
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31555050464D5545, 1, len(cut.ownership.sources)-2)
            for item in sources: writer.put('3Q4q', *item.generation)
            writer.catalog(sorted(part['fpga'] for part in assignment['partitions']))
            writer.catalog(cut.ownership.parts); writer.text(json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False))
            writer.finish()
        finally: writer.close()
        tool = resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool, [tool, 'phase3', 'publish-partition', *(str(item.path) for item in sources), str(request), str(output)],
                               environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode: raise ValidationError('native publication kernel failed: '+result.stdout)
        return json.loads((output/'phase3_report.json').read_text())
