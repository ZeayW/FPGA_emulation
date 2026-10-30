"""Current native PATRON v14 model/search contracts and small independent facts."""
import copy
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from emuflow.partition.partition import (CUT_MODE_STATIC_EXACT)
from tests.native_partition_facts import (compute_cut_nets, validate_partition_artifacts_online, build_clusters, build_partition_assignment, normalize_partition_constraints)
from emuflow_tools.stage_clients.partition import run_phase3
from emuflow.partition.phase3_storage import read_phase3_assignment_view
from tests.phase3_exact_fixture import indexed_exact_manifest
from emuflow.partition.combinational_cut import STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
from emuflow_tools.stage_clients.opensta import OPENSTA_PROVIDER
from emuflow.common.io import read_json, write_json
from tests.phase3_decision_reference import (PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
                                            _select_patron_timing_assignment)
from tests.partition_pressure_fixture import _endpoint, _ir, PressureFixture, prepare_native
from tests import partition_pressure_fixture as fixtures
from tests.native_build import emuflow_native, patron_ingress_oracle
from tests.patron_kernel_fixture import write_pressure_requests, run_pressure_kernel


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(emuflow_native().parent.parent))


class SmallPartitionFactsTest(unittest.TestCase):
    def setUp(self):
        fixture = PressureFixture()
        fixture.setUp()
        self.__dict__.update(fixture.__dict__)

    def test_undriven_wire_is_not_materialized_as_transport(self) -> None:
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "undriven_transport",
                    "top": "undriven_transport",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {
                        "id": "sink",
                        "type": "LUT1",
                        "resources": {"lut": 1},
                    }
                ],
                "nets": [
                    {
                        "id": "open_wire",
                        "cut_class": "undriven",
                        "drivers": [],
                        "sinks": [_endpoint("sink", "I")],
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )

        cuts, metrics = compute_cut_nets(ir, {"sink": "fpga2"})

        self.assertEqual(cuts, [])
        self.assertEqual(metrics["cut_nets"], 0)

    def test_v14_selects_changed_candidate_with_better_predictive_timing(self) -> None:
        initial = {
            "provider": "strong-tritonpart",
            "cluster_assignment": {"c0": "a", "c1": "b"},
        }
        candidate = copy.deepcopy(initial)
        candidate["provider"] = "patron-v14"
        candidate["cluster_assignment"] = {"c0": "b", "c1": "a"}
        trace = {
            "initial_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 8,
                "objective_key": [84.0, 189000.0, 5000, 4, 100, 10, 20, 10]
            },
            "final_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 7,
                "objective_key": [83.0, 180000.0, 4900, 4, 100, 10, 20, 10]
            },
        }
        selected, report = _select_patron_timing_assignment(
            initial, candidate, trace
        )
        self.assertIs(selected, candidate)
        self.assertEqual(report["selected"], "candidate")
        self.assertEqual(
            report["provider"], PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER
        )

    def test_v14_selection_ignores_downstream_semantic_diagnostics(self) -> None:
        initial = {
            "provider": "strong-tritonpart",
            "cluster_assignment": {"c0": "a", "c1": "b"},
            "semantic_contract": {
                "metrics": {
                    "logic_segments": 100,
                    "capture_requirements": 80,
                    "transported_cut_nets": 10,
                    "dependency_edges": 5,
                }
            },
        }
        candidate = copy.deepcopy(initial)
        candidate["provider"] = "patron-v14"
        candidate["cluster_assignment"] = {"c0": "b", "c1": "a"}
        candidate["semantic_contract"]["metrics"]["dependency_edges"] = 6
        trace = {
            "initial_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 8,
                "objective_key": [84.0, 189000.0, 5000, 4, 100, 10, 20, 10]
            },
            "final_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 7,
                "objective_key": [83.0, 180000.0, 4900, 4, 100, 10, 20, 10]
            },
        }
        selected, report = _select_patron_timing_assignment(
            initial, candidate, trace
        )
        self.assertIs(selected, candidate)
        self.assertEqual(report["selected"], "candidate")
        self.assertNotIn("semantic_non_regression", report)
        self.assertNotIn("initial_objective", report)
        self.assertNotIn("candidate_objective", report)

    def test_v14_does_not_require_deferred_semantic_contract(self) -> None:
        initial = {
            "provider": "strong-tritonpart",
            "cluster_assignment": {"c0": "a", "c1": "b"},
        }
        candidate = {
            "provider": "patron-v14",
            "cluster_assignment": {"c0": "b", "c1": "a"},
        }
        trace = {
            "initial_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 8,
                "objective_key": [84.0, 189000.0, 5000, 4, 100, 10, 20, 10]
            },
            "final_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 7,
                "objective_key": [83.0, 180000.0, 4900, 4, 100, 10, 20, 10]
            },
        }
        selected, report = _select_patron_timing_assignment(
            initial, candidate, trace
        )
        self.assertIs(selected, candidate)
        self.assertEqual(report["selected"], "candidate")
        self.assertNotIn("semantic_non_regression", report)
        self.assertNotIn("semantic_counts_available", report)



    def test_phase3_patron_v14_initializes_on_selected_generalized_graph(
        self,
    ) -> None:
        ir = _ir()
        ir.value["nets"][0]["cut_class"] = "combinational"
        ir.value["clocks"] = [
            {
                "id": "clk",
                "name": "clk",
                "source_port": "clk",
                "period_ns": None,
            }
        ]
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            ir,
            self.platform,
        )
        generalized_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        generalized_map = {
            cluster["id"]: (
                "a" if "u0" in cluster["instances"]
                else "b"
            )
            for cluster in generalized_clusters["clusters"]
        }
        generalized = build_partition_assignment(
            ir,
            self.platform,
            generalized_clusters,
            constraints,
            generalized_map,
            provider="fixture-generalized-tritonpart",
            seed=7,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.sqlite"
            platform_path = root / "platform.json"
            constraints_path = root / "constraints.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            from emuflow.ir.emuir_compact import write_compact_emuir
            write_compact_emuir(ir_path, ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(constraints_path, constraints)
            from emuflow.timing.sta import import_sta_path_database_tsv_streaming, STA_PATH_DATABASE_TSV_HEADER
            rows = [STA_PATH_DATABASE_TSV_HEADER]
            for i, path in enumerate(self.timing['paths']):
                identity = f"{path['startpoint']['instance']}/O->{path['endpoint']['instance']}/I#{i:08d}"
                rows.append('\t'.join([identity.encode().hex(), 'clk'.encode().hex(), str(path['clock_period_ns']),
                    str(path['slack_ns']), str(path['fixed_delay_ns']), ','.join(n.encode().hex() for n in path['path_nets'])]))
            tsv = root/'paths.tsv'; tsv.write_text('\n'.join(rows)+'\n')
            import_sta_path_database_tsv_streaming(tsv, ir_path, timing_path, provider=OPENSTA_PROVIDER)
            write_json(
                route_path,
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                    "tdm_ratio_quantum": 1,
                },
            )
            solution_path = root / "generalized.part.2"
            part_index = {"a": 0, "b": 1}
            solution_path.write_text(
                "\n".join(
                    str(part_index[generalized_map[cluster_id]])
                    for cluster_id in sorted(generalized_map)
                )
                + "\n",
                encoding="utf-8",
            )
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                constraints_path=constraints_path,
                provider="patron",
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(emuflow_native()),
                patron_algorithm_version=14,
                cut_mode=CUT_MODE_STATIC_EXACT,
                patron_max_moves=0,
                tritonpart_solution=solution_path,
            )
            assignment = read_json(root / "phase3/assignment.json")
            runtime = read_json(root / "phase3/phase3-runtime.json")
            self.assertEqual(
                report["patron_initialization"],
                "caller-supplied-generalized-tritonpart-solution-v1",
            )
            self.assertEqual(runtime['observations']['native_phase3_stage'], 1)
            self.assertNotIn("_derived_records", assignment)
            ownership_view = read_phase3_assignment_view(root/'phase3/assignment.json', include_instance_assignment=True)
            with closing(ownership_view['instance_assignment']) as owners:
                self.assertEqual(dict(owners), dict(generalized['instance_assignment']))
            self.assertGreater(
                assignment["metrics"]["combinational_cut_nets"], 0
            )
            self.assertEqual(
                report["algorithm_validation"]["status"], "pass"
            )
            self.assertEqual(report["patron_algorithm_version"], 14)
            self.assertEqual(
                report["algorithm_validation"][
                    "candidate_selection"
                ]["provider"],
                PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
            )

    def test_phase3_v14_explicit_solution_uses_static_exact_cold_start(self) -> None:
        ir = _ir()
        ir.value["nets"][0]["cut_class"] = "combinational"
        ir.value["clocks"] = [
            {
                "id": "clk",
                "name": "clk",
                "source_port": "clk",
                "period_ns": None,
            }
        ]
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            ir,
            self.platform,
        )
        static_exact_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        initial_map = {
            cluster["id"]: ("a" if index % 2 == 0 else "b")
            for index, cluster in enumerate(
                static_exact_clusters["clusters"]
            )
        }
        initial = build_partition_assignment(
            ir,
            self.platform,
            static_exact_clusters,
            constraints,
            initial_map,
            provider="fixture-static-exact",
            seed=7,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.sqlite"
            platform_path = root / "platform.json"
            constraints_path = root / "constraints.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            solution_path = root / "static-exact.part.2"
            from emuflow.ir.emuir_compact import write_compact_emuir
            write_compact_emuir(ir_path, ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(constraints_path, constraints)
            from emuflow.timing.sta import import_sta_path_database_tsv_streaming, STA_PATH_DATABASE_TSV_HEADER
            rows = [STA_PATH_DATABASE_TSV_HEADER]
            for i, path in enumerate(self.timing['paths']):
                identity = f"{path['startpoint']['instance']}/O->{path['endpoint']['instance']}/I#{i:08d}"
                rows.append('\t'.join([identity.encode().hex(), 'clk'.encode().hex(), str(path['clock_period_ns']),
                    str(path['slack_ns']), str(path['fixed_delay_ns']), ','.join(n.encode().hex() for n in path['path_nets'])]))
            tsv = root/'paths.tsv'; tsv.write_text('\n'.join(rows)+'\n')
            import_sta_path_database_tsv_streaming(tsv, ir_path, timing_path, provider=OPENSTA_PROVIDER)
            write_json(
                route_path,
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                    "tdm_ratio_quantum": 1,
                },
            )
            part = {"a": 0, "b": 1}
            solution_path.write_text(
                "\n".join(
                    str(part[initial_map[cluster_id]])
                    for cluster_id in sorted(initial_map)
                )
                + "\n",
                encoding="utf-8",
            )
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                constraints_path=constraints_path,
                provider="patron",
                tritonpart_solution=solution_path,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(emuflow_native()),
                patron_algorithm_version=14,
                cut_mode=CUT_MODE_STATIC_EXACT,
            )
            assignment = read_json(root / "phase3/assignment.json")
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["patron_initialization"],
                "caller-supplied-generalized-tritonpart-solution-v1",
            )
            self.assertNotIn('initial_assignment', report['algorithm_validation'])
            self.assertEqual(report['algorithm_validation']['scope'],
                             'production-source-native-independent-audit')
            self.assertEqual(
                assignment["metrics"]["cut_nets"],
                len(assignment["cut_nets"]),
            )
            self.assertNotIn('semantic_contract', assignment)
            self.assertTrue(assignment['semantic_contract_indexed'])
            manifest = indexed_exact_manifest(root/'phase3/assignment.sqlite')
            self.assertEqual(assignment['semantic_contract_schema'], manifest['contract_header']['schema'])
            self.assertEqual(assignment['semantic_contract_sha256'], manifest['semantic_contract_sha256'])
            self.assertEqual(manifest['contract_header']['mode'], CUT_MODE_STATIC_EXACT)


def case(root, kind='default'):
    root.mkdir(parents=True, exist_ok=True)
    if kind == 'default':
        source = PressureFixture()
        source.setUp()
        args = (source.ir, source.platform, source.clusters, source.constraints,
                source.timing, source.route_constraints, source.initial['cluster_assignment'])
    else:
        args = getattr(fixtures, kind + '_facts')()
    ir, platform, clusters, constraints, timing, routes, initial = args
    return prepare_native(root, ir, platform, constraints, timing, routes, clusters, initial)


def model_facts(value, root):
    root.mkdir()
    columns, board = write_pressure_requests(value.model,root, value.initial, 0)
    result = subprocess.run([str(patron_ingress_oracle()), '--facts', str(value.args[0].path),
        str(value.model.timing_path), str(value.model.primary_artifact), str(columns), str(board)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def checked(value, *, max_moves):
    final, trace = run_pressure_kernel(value.model,value.initial, max_moves=max_moves)
    assert trace['mode'] == 'endpoint-exact-critical-flow-v14'
    assert trace['native_input_transport'] == 'native-primary-store-model-v2'
    assert validate_partition_artifacts_online(value.args[1], value.args[2], final)['status'] == 'pass'
    assert _select_patron_timing_assignment(value.initial, final, trace)[1]['status'] == 'pass'
    return final, trace


def test_retired_python_pressure_producer_and_complete_model_tree_are_unavailable():
    assert importlib.util.find_spec('emuflow.partition_pressure') is None


def test_path_objective_uses_maximum_of_per_path_ratio_sums(tmp_path):
    value = case(tmp_path)
    final, trace = checked(value, max_moves=0)
    assert trace['initial_metrics']['maximum_path_hops'] == 1
    assert trace['initial_metrics']['maximum_path_tdm_ratio_sum'] == 2
    assert trace['initial_metrics']['total_path_partition_transitions'] == 2
    facts = model_facts(value, tmp_path/'facts')
    assert len(facts['paths']) == 2
    assert [row['period_ns'] for row in facts['paths']] == [10.0, 20.0]
    assert all(len(row['nets']) == 1 for row in facts['paths'])


def test_shared_bank_direction_pressure_is_not_a_partition_feasibility_gate(tmp_path):
    value = case(tmp_path, 'shared_bank')
    final, trace = checked(value, max_moves=0)
    assert trace['initial_metrics']['transport_capacity_violations'] == 1
    assert trace['initial_metrics']['transport_capacity_excess_bits'] == 1
    assert trace['final_metrics']['transport_capacity_violations'] <= 1
    assert final['cluster_assignment'] == value.initial['cluster_assignment']


def test_native_hard_capacity_consumes_source_sealed_reservations(tmp_path):
    source = PressureFixture()
    source.setUp()
    constraints = dict(source.constraints, resource_reservations_by_fpga={'a': {'lut': 7}})
    value = prepare_native(tmp_path, source.ir, source.platform, constraints, source.timing,
                          source.route_constraints, source.clusters, source.initial['cluster_assignment'])
    facts = model_facts(value, tmp_path/'facts')
    assert facts['dimensions'] == 2
    assert facts['hard_capacity'][0][1] == 93
    assert facts['hard_capacity'][1][1] == 100
    checked(value, max_moves=0)


def test_endpoint_exact_multicast_ignores_unrelated_remote_sink(tmp_path):
    exact = case(tmp_path/'exact', 'multicast')
    _, exact_trace = checked(exact, max_moves=0)
    facts = model_facts(exact, tmp_path/'facts')
    assert len(facts['nets']) == 1 and len(facts['paths']) == 2
    assert len(facts['nets'][0]['sinks']) == 3
    assert len({row['end_cluster'] for row in facts['paths']}) == 2
    assert exact_trace['initial_metrics']['maximum_path_hops'] == 2
    assert exact_trace['initial_metrics']['total_path_partition_transitions'] == 1
    raw = list(fixtures.multicast_facts())
    raw[4] = copy.deepcopy(raw[4])
    for path in raw[4]['paths']:
        path.pop('startpoint')
        path.pop('endpoint')
    root = tmp_path/'conservative'
    root.mkdir()
    ir, platform, clusters, constraints, timing, routes, initial = raw
    conservative = prepare_native(root, ir, platform, constraints, timing, routes, clusters, initial)
    _, conservative_trace = checked(conservative, max_moves=0)
    assert conservative_trace['initial_metrics']['total_path_partition_transitions'] > 1
    assert all(path['start_cluster'] == -1 and path['end_cluster'] == -1
               for path in model_facts(conservative, tmp_path/'conservative-facts')['paths'])


@pytest.mark.parametrize('kind,budget', [('sparse_chain',64), ('capacity_release',8),
                                         ('balanced_ejection',4), ('third_block_ejection',4)])
def test_retained_native_sweeps_and_capacity_release_improve_timing(tmp_path, kind, budget):
    value = case(tmp_path, kind)
    with patch.dict(os.environ, {'EMUFLOW_PATRON_FLOW_APPLY': '1', 'EMUFLOW_PATRON_FLOW_DISTANCE': '999999'}):
        final, trace = checked(value, max_moves=budget)
    # ABI change_count counts batch members; independent moves/ejections use move_count.
    assert trace['move_count'] + trace['change_count'] > 0
    assert final['cluster_assignment'] != value.initial['cluster_assignment']
    _, selection = _select_patron_timing_assignment(value.initial, final, trace)
    assert selection['timing_improved'] is True
    if kind == 'capacity_release':
        # This case cannot improve the second constrained pair without revisiting
        # it after a capacity-releasing move in an earlier sweep.
        assert final['instance_assignment']['u00003'] == 'b'
        assert final['instance_assignment']['u00001'] == 'a'
    if kind == 'balanced_ejection':
        from collections import Counter
        assert Counter(final['instance_assignment'].values()) == {'a': 150, 'b': 150}
        assert final['instance_assignment']['u00000'] == final['instance_assignment']['u00001']
    if kind == 'third_block_ejection':
        assert final['instance_assignment']['u00000'] == 'b'
        assert final['instance_assignment']['u00004'] == 'c'


def test_v14_keeps_maximum_path_hops_ahead_of_scalar_predictive_slack(tmp_path):
    value = case(tmp_path, 'path_hop_priority')
    final, trace = checked(value, max_moves=8)
    assert trace['final_metrics']['maximum_path_hops'] <= trace['initial_metrics']['maximum_path_hops']
    assert final['instance_assignment']['u1'] == 'b'
    assert 'maximum_path_tdm_ratio_sum' in trace['final_metrics']
    assert 'total_path_partition_transitions' in trace['initial_metrics']


@pytest.mark.parametrize('argument', [0, -1, True, 0.5])
def test_current_native_stage_rejects_invalid_timeout_without_publication(tmp_path, argument):
    from emuflow_tools.stage_clients.partition_native import run as run_stage
    value = case(tmp_path/'facts')
    ir, platform, clusters, constraints, _, routes = value.args
    for name, document in (('board', platform.to_dict()), ('constraints', constraints), ('routes', routes)):
        write_json(tmp_path/(name+'.json'), document)
    parts = [fpga.id for fpga in platform.fpgas if fpga.id in constraints['active_fpgas']]
    solution = tmp_path/'solution.txt'
    solution.write_text(''.join(str(parts.index(value.initial['cluster_assignment'][f'c{i:06d}']))+'\n'
        for i in range(len(clusters['clusters']))))
    output = tmp_path/'rejected'
    with pytest.raises(ValidationError):
        run_stage(output, ir.path, tmp_path/'board.json', constraints_path=tmp_path/'constraints.json',
            route_constraints_path=tmp_path/'routes.json', timing_database_path=value.timing,
            tritonpart_solution=solution, tritonpart_timeout_seconds=argument)
    assert not output.exists()


@pytest.mark.parametrize('direction,expected_domains,expected_ratio', [
    ('full_duplex', 2, 1), ('half_duplex', 1, 2),
])
def test_opposite_unicast_paths_obey_full_and_half_duplex_capacity(
        tmp_path, direction, expected_domains, expected_ratio):
    from emuflow.platform.platform import Platform
    source = PressureFixture()
    source.setUp()
    value = source.platform.to_dict()
    value['links'][0]['direction'] = direction
    platform = Platform.from_dict(value)
    by_instance = {member: cluster['id'] for cluster in source.clusters['clusters'] for member in cluster['instances']}
    initial = dict(source.initial['cluster_assignment'])
    initial[by_instance['u2']], initial[by_instance['u3']] = 'b', 'a'
    routes = fixtures.normalize_route_constraints({'schema': 'emuflow.system-route-constraints/v1',
        'frame_slots': 8, 'tdm_ratio_quantum': 1, 'max_route_hops': 1}, platform)
    prepared = prepare_native(tmp_path, source.ir, platform, source.constraints, source.timing,
                              routes, source.clusters, initial)
    facts = model_facts(prepared, tmp_path/'facts')
    assert len(facts['domains']) == expected_domains
    _, trace = checked(prepared, max_moves=0)
    assert trace['initial_metrics']['maximum_predicted_tdm_ratio'] == expected_ratio
    assert trace['initial_metrics']['maximum_capacity_domain_load'] == expected_ratio
