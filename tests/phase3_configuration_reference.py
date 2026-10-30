"""Independent bounded partition-configuration oracle, never installed."""
import json
from pathlib import Path
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import require_native_schema,resolve_native_executable
from emuflow.storage.store_publication import bind_store_source,require_store_sources_unchanged


def write_request(directory, ir_path, platform_path, *, constraints_path=None,
        seed=0, min_used_fpgas=None, balance_tolerance=None, provider='patron', openroad=None,
        tritonpart_solution=None, net_weights_path=None, tritonpart_timeout_seconds=3600,
        tritonpart_seed_attempts=1, tritonpart_num_initial_solutions=50,
        tritonpart_num_best_initial_solutions=10, tritonpart_repair_min_used_fpgas=True,
        tritonpart_run_unweighted_baseline=False, route_constraints_path=None,
        max_cross_fpga_dependency_depth=0, static_exact_candidate_policy=None,
        timing_database_path=None, patron_max_moves=None):
    from emuflow.platform.platform import Platform
    from emuflow.partition.partition_ir import load_partition_ir
    from tests.phase3_policy_reference import write_candidate_policy
    from emuflow.partition.structural_policy import _pattern_range_policy
    from emuflow.partition.partition import LEGAL_CUT_CLASSES, REPLICATED_NET_CLASSES, HARD_MACRO_RESOURCES
    from emuflow.partition.partition_topology import _shortest_routes, _capacity_key_by_arc
    from emuflow.routing.routing import build_directed_graph, route_link_delay_coefficients_ns, route_link_delay_ns
    from tests.routing_validation_reference import load_route_constraints
    from emuflow.timing.sta import sta_path_database_payload_path, partition_net_weights_payload_path
    from emuflow.timing.sta_weights_native import _selector
    from emuflow.partition.combinational_cut import _canonical_sha256, STATIC_EXACT_DEFAULT_CANDIDATE_POLICY

    require_native_schema('phase3-stage-request-v1')
    directory = Path(directory);directory.mkdir(parents=True, exist_ok=True)
    source = bind_store_source(Path(ir_path).resolve(), 'emuir-compact-v2')
    board = bind_store_source(Path(platform_path).resolve(), 'boarddb-v1')
    ir = load_partition_ir(source.path)  # Metadata/count view, no source population.
    platform = Platform.load(board.path)
    policy = directory/'candidate-policy.bin'
    if write_candidate_policy(policy, ir) != source.generation:
        raise ValidationError('native Phase 3 source changed during configuration')
    sources = {'source': source, 'board': board,
               'candidate_policy': bind_store_source(policy, 'candidate-policy-request-v1'),
               'native': bind_store_source(Path(resolve_native_executable('emuflow-native')), 'external-tool-executable-v1')}
    if constraints_path is not None:
        sources['constraints'] = bind_store_source(Path(constraints_path).resolve(), 'partition-constraints-input-v1')
    if timing_database_path is not None:
        manifest, selector = _selector(Path(timing_database_path).resolve(), 'sta-path-selector-v1')
        payload = sta_path_database_payload_path(timing_database_path, manifest)
        if payload is None:
            raise ValidationError('native Phase 3 requires indexed timing paths')
        sources['timing_selector'] = selector
        sources['timing'] = bind_store_source(payload, 'timing-path-store-v1')
    if net_weights_path is not None:
        manifest, selector = _selector(Path(net_weights_path).resolve(), 'net-weight-selector-v2')
        payload = partition_net_weights_payload_path(net_weights_path, manifest)
        if payload is None:
            raise ValidationError('native Phase 3 requires indexed net weights')
        sources['weight_selector'] = selector
        sources['weights'] = bind_store_source(payload, 'net-weight-store-v1')
    if tritonpart_solution is not None:
        sources['solution'] = bind_store_source(Path(tritonpart_solution).resolve(), 'tritonpart-solution-v1')
    routes = load_route_constraints(route_constraints_path, platform)
    _, _, capacities = build_directed_graph(platform, routes, reserve_arrival_and_commit=True)
    links = {link.id: link for link in platform.links}
    ordered = sorted(capacities.items());domain_index = {key: i for i, (key, _) in enumerate(ordered)}
    model = {'capacities': [value for _, value in ordered], 'shortest_routes': _shortest_routes(platform, routes)}
    arc_domain = _capacity_key_by_arc(model)
    domains = []
    for key, value in ordered:
        link = links[value['link']]
        domains.append({'key': key, 'lanes': link.transport_bits_per_cycle_per_direction,
            'sll': link.id in routes['sll_links'],
            'slope': route_link_delay_coefficients_ns(platform, link.id, *link.endpoints, routes)[1],
            'opposite': domain_index.get(value.get('opposite_capacity_key'), -1),
            'bank_count': value.get('bank_count', 0), 'wires_per_bank': value.get('wires_per_bank', 0),
            'capacity_bits': int(value['capacity_bits'])})
    shortest = {a: {b: None if path is None else [[domain_index[arc_domain[(arc['link'], arc['from'], arc['to'])]],
        route_link_delay_ns(platform, arc['link'], arc['from'], arc['to'], routes)] for arc in path]
        for b, path in row.items()} for a, row in model['shortest_routes'].items()}
    from emuflow.partition.partition_topology import _shortest_hop_distances
    configuration = {'schema': 'emuflow.phase3-stage/v1',
        'inputs': {name: {'path': str(item.path), 'schema': item.schema, 'generation': list(item.generation)}
                   for name, item in sources.items()},
        'platform': platform.name, 'platform_sha256': _canonical_sha256(platform.to_dict()),
        'partition_configuration': {'pattern_range_policy': _pattern_range_policy(),
            'min_used_fpgas': min_used_fpgas, 'balance_tolerance': balance_tolerance,
            'board': {'fpgas': [{'id': fpga.id, 'effective_capacity': fpga.effective_capacity} for fpga in platform.fpgas],
                'links': [{'from': link.endpoints[0], 'to': link.endpoints[1],
                    'capacity': link.transport_bits_per_cycle_per_direction, 'latency': link.latency_cycles,
                    'bidirectional': link.direction in {'full_duplex', 'half_duplex'}} for link in platform.links]}},
        'cluster_policy': {'legal_cut_classes': sorted(LEGAL_CUT_CLASSES),
            'replicated_net_classes': sorted(REPLICATED_NET_CLASSES), 'hard_macro_resources': sorted(HARD_MACRO_RESOURCES),
            'hard_macro_granularity': 'instance', 'cut_mode': 'static-exact-combinational',
            'max_cross_fpga_dependency_depth': max_cross_fpga_dependency_depth,
            'transported_cut_classes': ['combinational', 'register_input', 'register_output'],
            'qualification': 'structural-partition-legality'},
        'candidate_selection_policy': static_exact_candidate_policy or STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
        'provider': provider, 'seed': seed, 'openroad': '',
        'timeout_seconds': tritonpart_timeout_seconds, 'seed_attempts': tritonpart_seed_attempts,
        'num_initial_solutions': tritonpart_num_initial_solutions,
        'num_best_initial_solutions': tritonpart_num_best_initial_solutions,
        'repair_min_used': tritonpart_repair_min_used_fpgas, 'unweighted_baseline': tritonpart_run_unweighted_baseline,
        'max_moves': patron_max_moves, 'route_constraints': routes, 'domains': domains, 'shortest_routes': shortest,
        'hop_distances': _shortest_hop_distances(platform, routes)}
    if tritonpart_solution is None:
        from emuflow.common.errors import EmuFlowError
        try:
            configuration['openroad'] = resolve_native_executable('openroad', openroad)
        except EmuFlowError:
            if openroad is not None:
                raise
            # The source-backed capacity selector may leave one active target.
            # The native initializer rejects missing engines if a search is needed.
    encoded = json.dumps(configuration, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > 8*1024*1024:
        raise ValidationError('native Phase 3 request exceeds bounded policy contract')
    require_store_sources_unchanged(sources.values())
    request = directory/'phase3-request.json'
    request.write_text(encoded+'\n')
    return request
