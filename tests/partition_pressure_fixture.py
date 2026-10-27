"""Small input data and current typed-store preparation; no pressure algorithm."""
import copy
from emuflow.ir import EmuIR
from emuflow.partition import build_clusters, build_partition_assignment, normalize_partition_constraints, CUT_MODE_STATIC_EXACT
from emuflow.combinational_cut import STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from tests.test_phase5 import _link, _platform_value


def _endpoint(instance: str, port: str) -> dict:
    return {"instance": instance, "port": port, "bit": 0}


def _ir() -> EmuIR:
    instances = [
        {
            "id": f"u{index}",
            "type": "LUT1",
            "resources": {"lut": 1},
        }
        for index in range(4)
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure",
                "top": "pressure",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": instances,
            "nets": [
                {
                    "id": "critical",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u0", "O")],
                    "sinks": [_endpoint("u1", "I")],
                },
                {
                    "id": "relaxed",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u2", "O")],
                    "sinks": [_endpoint("u3", "I")],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
    )


def _chain_ir(instances: int) -> EmuIR:
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure_chain",
                "top": "pressure_chain",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": [
                {
                    "id": f"u{index:05d}",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                }
                for index in range(instances)
            ],
            "nets": [
                {
                    "id": f"n{index:05d}",
                    "cut_class": "register_output",
                    "drivers": [_endpoint(f"u{index:05d}", "O")],
                    "sinks": [_endpoint(f"u{index + 1:05d}", "I")],
                }
                for index in range(instances - 1)
            ],
            "clocks": [],
            "warnings": [],
        }
    )


def _capacity_release_ir(instances: int = 300) -> EmuIR:
    """Large sparse graph whose second improving move needs another sweep."""

    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure_capacity_release",
                "top": "pressure_capacity_release",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": [
                {
                    "id": f"u{index:05d}",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                }
                for index in range(instances)
            ],
            "nets": [
                {
                    "id": "n00000",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u00000", "O")],
                    "sinks": [_endpoint("u00001", "I")],
                },
                {
                    "id": "n00002",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u00002", "O")],
                    "sinks": [_endpoint("u00003", "I")],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
    )


class PressureFixture:
    def setUp(self) -> None:
        self.ir = _ir()
        self.platform = Platform.from_dict(
            _platform_value(
                "pressure_platform",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        self.constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        self.clusters = build_clusters(self.ir, self.constraints, cut_mode="static-exact-combinational")
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in self.clusters["clusters"]
        }
        self.initial = build_partition_assignment(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "a",
                by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=7,
        )
        self.route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            self.platform,
        )
        self.timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 20.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u0", "O"),
                    "endpoint": _endpoint("u1", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["critical"],
                    "normalized_slack": 0.075,
                },
                {
                    "id": "p-relaxed",
                    "startpoint": _endpoint("u2", "O"),
                    "endpoint": _endpoint("u3", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 20.0,
                    "fixed_delay_ns": 0.0,
                    "path_nets": ["relaxed"],
                    "normalized_slack": 1.0,
                },
            ],
        }


def prepare_native(root, reference, platform, constraints, timing, routes, original_clusters, original_assignment):
    """Serialize small input facts to the current source-owned test boundary."""
    import json
    from types import SimpleNamespace
    from emuflow.emuir_compact import write_compact_emuir
    from emuflow.partition_ir import load_partition_ir
    from tests.native_partition_kernel_driver import initialize
    from emuflow.sta import STA_PATH_DATABASE_TSV_HEADER, import_sta_path_database_tsv_streaming
    from emuflow.opensta import OPENSTA_PROVIDER
    from emuflow.io import read_json
    from emuflow.phase3_pressure_native import build_native_pressure_model
    source = root / 'source.sqlite'
    write_compact_emuir(source, reference.value)
    ir = load_partition_ir(source)
    constraints = normalize_partition_constraints(constraints, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational', _native_compact_clusters=True)
    by_instance = {member: original_assignment[cluster['id']]
                   for cluster in original_clusters['clusters'] for member in cluster['instances']}
    parts = [fpga.id for fpga in platform.fpgas if fpga.id in constraints['active_fpgas']]
    solution = root / 'initial.txt'
    targets = []
    for cluster in clusters['clusters']:
        selected = {by_instance[member] for member in cluster['instances']}
        assert len(selected) == 1
        targets.append(parts.index(selected.pop()))
    solution.write_text(''.join(str(part)+'\n' for part in targets))
    owners, _ = initialize(ir, platform, clusters, constraints, seed=7, executable=None, solution_input=solution)
    initial = build_partition_assignment(ir, platform, clusters, constraints, owners, 'fixture', 7,
        _include_semantic_contract=False, _stream_semantic_records=True, _defer_derived_records=True)
    rows = [STA_PATH_DATABASE_TSV_HEADER]
    for seq, path in enumerate(timing['paths']):
        if path.get('startpoint') is not None and path.get('endpoint') is not None:
            start, end = path['startpoint'], path['endpoint']
            identity = f"{start['instance']}/{start['port']}->{end['instance']}/{end['port']}#{seq:08d}"
        else:
            identity = f'conservative-{seq}'
        rows.append('\t'.join([identity.encode().hex(), path['clock_domain'].encode().hex(),
            str(path['clock_period_ns']), str(path['slack_ns']), str(path['fixed_delay_ns']),
            ','.join(net.encode().hex() for net in path['path_nets'])]))
    measured = root / 'timing.tsv'
    measured.write_text('\n'.join(rows)+'\n')
    selector = root / 'timing.json'
    import_sta_path_database_tsv_streaming(measured, source, selector, provider=OPENSTA_PROVIDER)
    args = ir, platform, clusters, constraints, read_json(selector), routes
    return SimpleNamespace(args=args, initial=initial, model=build_native_pressure_model(
        *args, timing_database_path=selector), timing=selector, reference=reference)


def sparse_chain_facts():
    self = PressureFixture()
    self.setUp()
    ir = _chain_ir(300)
    value = _platform_value('pressure_chain_platform', ['a', 'b'], [_link('ab', 'a', 'b', lanes=8, latency=1)])
    for fpga in value['fpgas']:
        fpga['capacity'] = {'lut': 1000, 'ff': 1000}
    platform = Platform.from_dict(value)
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'min_used_fpgas': 2, 'balance_tolerance': 0.25}, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational')
    initial = build_partition_assignment(ir, platform, clusters, constraints, {cluster['id']: 'a' if index % 2 == 0 else 'b' for (index, cluster) in enumerate(clusters['clusters'])}, provider='fixture', seed=1)
    route_constraints = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 64, 'tdm_ratio_quantum': 1, 'max_route_hops': 1}, platform)
    timing = {'schema': 'emuflow.sta-path-database/v1', 'design': 'pressure_chain', 'source': {'provider': 'fixture', 'input': 'fixture'}, 'normalization': {'positive_slack_scale_ns': 3.0, 'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': 10.0}, 'paths': [{'id': f'p{index:05d}', 'startpoint': _endpoint(f'u{index:05d}', 'O'), 'endpoint': _endpoint(f'u{index + 1:05d}', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 3.0, 'fixed_delay_ns': 7.0, 'path_nets': [f'n{index:05d}'], 'normalized_slack': 1.0} for index in range(299)]}
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)


def capacity_release_facts():
    self = PressureFixture()
    self.setUp()
    ir = _capacity_release_ir()
    value = _platform_value('pressure_capacity_release_platform', ['a', 'b'], [_link('ab', 'a', 'b', lanes=8, latency=1)])
    for fpga in value['fpgas']:
        fpga['capacity'] = {'lut': 1000, 'ff': 1000}
    platform = Platform.from_dict(value)
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'fixed': [{'instance': 'u00000', 'fpga': 'a'}, {'instance': 'u00002', 'fpga': 'b'}], 'min_used_fpgas': 2, 'balance_dimensions': ['cells', 'lut', 'ff'], 'balance_tolerance': 0.01}, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational')
    assert len(clusters['clusters']) == 300
    by_instance = {cluster['instances'][0]: cluster['id'] for cluster in clusters['clusters']}
    mapping = {cluster['id']: 'a' if int(cluster['instances'][0][1:]) % 2 == 0 else 'b' for cluster in clusters['clusters']}
    mapping[by_instance['u00002']] = 'b'
    mapping[by_instance['u00003']] = 'a'
    mapping[by_instance['u00005']] = 'a'
    initial = build_partition_assignment(ir, platform, clusters, constraints, mapping, provider='fixture', seed=1)
    route_constraints = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 64, 'tdm_ratio_quantum': 1, 'max_route_hops': 1}, platform)
    timing = {'schema': 'emuflow.sta-path-database/v1', 'design': 'pressure_capacity_release', 'source': {'provider': 'fixture', 'input': 'fixture'}, 'normalization': {'positive_slack_scale_ns': 3.0, 'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': 10.0}, 'paths': [{'id': f'p{index:05d}', 'startpoint': _endpoint(f'u{index:05d}', 'O'), 'endpoint': _endpoint(f'u{index + 1:05d}', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 3.0, 'fixed_delay_ns': 7.0, 'path_nets': [f'n{index:05d}'], 'normalized_slack': 1.0} for index in (0, 2)]}
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)


def balanced_ejection_facts():
    self = PressureFixture()
    self.setUp()
    ir = _capacity_release_ir()
    value = _platform_value('pressure_ejection_platform', ['a', 'b'], [_link('ab', 'a', 'b', lanes=8, latency=1)])
    for fpga in value['fpgas']:
        fpga['capacity'] = {'lut': 1000, 'ff': 1000}
    platform = Platform.from_dict(value)
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'min_used_fpgas': 2, 'balance_dimensions': ['cells', 'lut', 'ff'], 'balance_tolerance': 0.0}, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational')
    mapping = {cluster['id']: 'a' if int(cluster['instances'][0][1:]) % 2 == 0 else 'b' for cluster in clusters['clusters']}
    initial = build_partition_assignment(ir, platform, clusters, constraints, mapping, provider='fixture', seed=1)
    routes = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 64, 'tdm_ratio_quantum': 1, 'max_route_hops': 1}, platform)
    timing = {'schema': 'emuflow.sta-path-database/v1', 'design': 'pressure_capacity_release', 'source': {'provider': 'fixture', 'input': 'fixture'}, 'normalization': {'positive_slack_scale_ns': 3.0, 'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': 10.0}, 'paths': [{'id': 'p-critical', 'startpoint': _endpoint('u00000', 'O'), 'endpoint': _endpoint('u00001', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 3.0, 'fixed_delay_ns': 7.0, 'path_nets': ['n00000'], 'normalized_slack': 1.0}]}
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)


def third_block_ejection_facts():
    self = PressureFixture()
    self.setUp()
    raw_ir = copy.deepcopy(_capacity_release_ir().value)
    raw_ir['design']['name'] = 'pressure_third_block_ejection'
    raw_ir['design']['top'] = 'pressure_third_block_ejection'
    raw_ir['nets'] = raw_ir['nets'][:1]
    resources = {'u00000': {'lut': 10}, 'u00001': {'lut': 0}, 'u00003': {'lut': 1, 'ff': 10}, 'u00004': {'lut': 10, 'ff': 10}}
    for instance in raw_ir['instances']:
        instance['resources'] = resources.get(instance['id'], {'lut': 1})
    ir = EmuIR(raw_ir)
    value = _platform_value('pressure_third_block_ejection_platform', ['a', 'b', 'c'], [_link('ab', 'a', 'b', lanes=8, latency=1), _link('ac', 'a', 'c', lanes=8, latency=50), _link('bc', 'b', 'c', lanes=8, latency=50)])
    capacities = {'a': {'lut': 120, 'ff': 10}, 'b': {'lut': 108, 'ff': 20}, 'c': {'lut': 120, 'ff': 20}}
    for fpga in value['fpgas']:
        fpga['capacity'] = capacities[fpga['id']]
    platform = Platform.from_dict(value)
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'fixed': [{'instance': 'u00001', 'fpga': 'b'}], 'min_used_fpgas': 3, 'balance_tolerance': 2.0}, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational')
    by_instance = {cluster['instances'][0]: cluster['id'] for cluster in clusters['clusters']}
    parts = ('a', 'b', 'c')
    mapping = {cluster['id']: parts[int(cluster['instances'][0][1:]) % len(parts)] for cluster in clusters['clusters']}
    initial = build_partition_assignment(ir, platform, clusters, constraints, mapping, provider='fixture', seed=1)
    routes = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 64, 'tdm_ratio_quantum': 1, 'max_route_hops': 1}, platform)
    timing = {'schema': 'emuflow.sta-path-database/v1', 'design': 'pressure_third_block_ejection', 'source': {'provider': 'fixture', 'input': 'fixture'}, 'normalization': {'positive_slack_scale_ns': 3.0, 'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': 10.0}, 'paths': [{'id': 'p-critical', 'startpoint': _endpoint('u00000', 'O'), 'endpoint': _endpoint('u00001', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 3.0, 'fixed_delay_ns': 7.0, 'path_nets': ['n00000'], 'normalized_slack': 1.0}]}
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)


def path_hop_priority_facts():
    self = PressureFixture()
    self.setUp()
    ir = EmuIR({'schema': 'emuflow.emuir/v1', 'design': {'name': 'pressure_static_exact_guard', 'top': 'pressure_static_exact_guard', 'source_format': 'fixture'}, 'ports': [], 'instances': [{'id': f'u{index}', 'type': 'LUT1', 'resources': {'lut': 1}} for index in range(4)], 'nets': [{'id': 'architectural-relaxed', 'cut_class': 'register_output', 'drivers': [_endpoint('u0', 'O')], 'sinks': [_endpoint('u1', 'I')]}, {'id': 'architectural-critical', 'cut_class': 'register_output', 'drivers': [_endpoint('u1', 'O')], 'sinks': [_endpoint('u2', 'I')]}], 'clocks': [{'id': 'clk', 'name': 'clk', 'source_port': 'clk', 'period_ns': None}], 'warnings': []})
    platform = Platform.from_dict(_platform_value('pressure_static_exact_guard_platform', ['a', 'b', 'c'], [_link('ab', 'a', 'b', lanes=8, latency=1), _link('bc', 'b', 'c', lanes=8, latency=1)]))
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'min_used_fpgas': 3, 'balance_tolerance': 2.0}, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode=CUT_MODE_STATIC_EXACT, static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2)
    by_instance = {cluster['instances'][0]: cluster['id'] for cluster in clusters['clusters']}
    initial = build_partition_assignment(ir, platform, clusters, constraints, {by_instance['u0']: 'a', by_instance['u1']: 'b', by_instance['u2']: 'c', by_instance['u3']: 'b'}, provider='fixture', seed=1)
    routes = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 64, 'tdm_ratio_quantum': 1, 'max_route_hops': 2}, platform)
    timing = {'schema': 'emuflow.sta-path-database/v1', 'design': 'pressure_static_exact_guard', 'source': {'provider': 'fixture', 'input': 'fixture'}, 'normalization': {'positive_slack_scale_ns': 20.0, 'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': 20.0}, 'paths': [{'id': 'p-relaxed', 'startpoint': _endpoint('u0', 'O'), 'endpoint': _endpoint('u1', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 100.0, 'fixed_delay_ns': 0.0, 'path_nets': ['architectural-relaxed'], 'normalized_slack': 5.0}, {'id': 'p-critical', 'startpoint': _endpoint('u1', 'O'), 'endpoint': _endpoint('u2', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': -10.0, 'fixed_delay_ns': 20.0, 'path_nets': ['architectural-critical'], 'normalized_slack': -1.0}]}
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)


def multicast_facts():
    self = PressureFixture()
    self.setUp()
    ir = EmuIR({'schema': 'emuflow.emuir/v1', 'design': {'name': 'fanout_pressure', 'top': 'fanout_pressure', 'source_format': 'fixture'}, 'ports': [], 'instances': [{'id': item, 'type': 'LUT1', 'resources': {'lut': 1}} for item in ('driver', 'local', 'remote', 'remote_peer')], 'nets': [{'id': 'fanout', 'cut_class': 'register_output', 'drivers': [_endpoint('driver', 'O')], 'sinks': [_endpoint('local', 'I'), _endpoint('remote', 'I'), _endpoint('remote_peer', 'I')]}], 'clocks': [], 'warnings': []})
    platform = Platform.from_dict(_platform_value('fanout_platform', ['a', 'b', 'c'], [_link('ab', 'a', 'b', lanes=1, latency=1), _link('bc', 'b', 'c', lanes=1, latency=1)]))
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'min_used_fpgas': 2, 'balance_tolerance': 2.0}, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational')
    cluster_by_instance = {cluster['instances'][0]: cluster['id'] for cluster in clusters['clusters']}
    assignment = {cluster_by_instance['driver']: 'a', cluster_by_instance['local']: 'a', cluster_by_instance['remote']: 'c', cluster_by_instance['remote_peer']: 'c'}
    routes = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 8, 'tdm_ratio_quantum': 1, 'max_route_hops': 2}, platform)
    timing = {'schema': 'emuflow.sta-path-database/v1', 'design': 'fanout_pressure', 'source': {'provider': 'fixture', 'input': 'fixture'}, 'normalization': {'positive_slack_scale_ns': 10.0, 'negative_slack_scale_ns': 1.0, 'max_clock_period_ns': 10.0}, 'paths': [{'id': 'to-local', 'startpoint': _endpoint('driver', 'O'), 'endpoint': _endpoint('local', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 5.0, 'fixed_delay_ns': 5.0, 'path_nets': ['fanout'], 'normalized_slack': 0.5}, {'id': 'to-remote', 'startpoint': _endpoint('driver', 'O'), 'endpoint': _endpoint('remote', 'I'), 'clock_domain': 'clk', 'clock_period_ns': 10.0, 'slack_ns': 5.0, 'fixed_delay_ns': 5.0, 'path_nets': ['fanout'], 'normalized_slack': 0.5}]}
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)


def shared_bank_facts():
    self = PressureFixture()
    self.setUp()
    value = _platform_value('pressure_shared_bank_platform', ['a', 'b'], [_link('ab', 'a', 'b', lanes=1, latency=0)])
    for fpga in value['fpgas']:
        fpga['hp_io_bank_count'] = 1
    value['links'][0].update({'capacity_sharing': 'shared_bidirectional', 'bank_count': 1, 'wires_per_bank': 1})
    platform = Platform.from_dict(value)
    constraints = normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1', 'min_used_fpgas': 2, 'balance_tolerance': 1.0}, self.ir, platform)
    clusters = build_clusters(self.ir, constraints, cut_mode='static-exact-combinational')
    by_instance = {cluster['instances'][0]: cluster['id'] for cluster in clusters['clusters']}
    initial = build_partition_assignment(self.ir, platform, clusters, constraints, {by_instance['u0']: 'a', by_instance['u1']: 'b', by_instance['u2']: 'a', by_instance['u3']: 'b'}, provider='fixture', seed=7)
    routes = normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 2, 'tdm_ratio_quantum': 1, 'max_route_hops': 1}, platform)
    facts = locals()
    selected = facts.get('initial', facts.get('assignment'))
    if 'cluster_assignment' in selected:
        selected = selected['cluster_assignment']
    return (facts.get('ir', self.ir), platform, clusters, constraints,
            facts.get('timing', self.timing), facts.get('routes', facts.get('route_constraints', self.route_constraints)), selected)
