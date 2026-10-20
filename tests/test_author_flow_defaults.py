"""Omitted defaults, no-timing and zero-demand author flow boundaries."""
import copy
from pathlib import Path
import tempfile
import unittest

from emuflow.author_routing import AUTHOR_ROUTER_PROVIDER
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.cross_stage import run_cross_stage_optimization
from emuflow.frame_search import run_frame_length_search, validate_frame_search_report
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import run_multi_fpga_flow, validate_multi_fpga_flow_bundle
from emuflow.tdm import TDM_BASELINE_PROVIDER
from emuflow.timing_routing import NATIVE_TIMING_EVALUATED_PROVIDER
from tests.native_build import tlr_router
from tests.test_author_routing import author_router
from tests import test_author_flow_modes as mode_fixtures

ROOT = Path(__file__).resolve().parents[1]


class AuthorFlowDefaultsTest(unittest.TestCase):
    def _flow(self, root, single=False, **options):
        constraints = None
        if single:
            # Explicit placement creates only this unit fixture's empty boundary.
            constraints = root / 'constraints.json'
            write_json(constraints, {'schema': 'emuflow.partition-constraints/v1',
                'fixed': [{'patterns': ['*'], 'fpga': 'fpga0'}],
                'min_used_fpgas': 1, 'balance_tolerance': 3.0})
        settings = dict(platform_path=ROOT/'platforms/virtual/academic_vtr_2fpga_p2p.json',
            output_dir=root/'flow', yosys_json=ROOT/'examples/yosys/counter.json',
            top='counter', clocks=['clk'], partition_provider='greedy',
            partition_constraints=constraints, timing_driven=False, clock_periods={'clk': 10.0},
            opensta=str(ROOT/'tests/fixtures/fake_opensta_paths.py'), router=author_router(),
            frame_slots=32, equivalence_cycles=2, mapping_profile='vtr-hard-blocks')
        settings.update(options)
        report = run_multi_fpga_flow(**settings)
        return report, read_json(root/'flow/system-route/routes.json'), read_json(root/'flow/tdm/schedule.json')

    def test_omitted_and_none_no_timing_defaults_match_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs=[]
            for name,options in [('omitted',{}),('none',dict(route_provider=None,author_cost_policy=None,author_order_policy=None))]:
                root=Path(tmp)/name;root.mkdir()
                report,routes,schedule=self._flow(root,**options)
                self.assertEqual(report['author_cost_policy'],'author-pressure-v1')
                self.assertEqual(report['author_order_policy'],'hard-connections-v1')
                self.assertEqual(routes['provider'],AUTHOR_ROUTER_PROVIDER)
                self.assertFalse(routes['author_routing']['timing_optimization'])
                self.assertTrue(routes['timing']['paths'])
                self.assertTrue(routes['routes'])
                self.assertEqual(schedule['provider'],TDM_BASELINE_PROVIDER)
                self.assertEqual(validate_multi_fpga_flow_bundle(root/'flow')['status'],'pass')
                outputs.append((routes['routes'],schedule['entries']))
            self.assertEqual(outputs[0],outputs[1])

    def test_empty_default_author_replays_without_projected_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);report,routes,_=self._flow(root,single=True)
            self.assertEqual(routes['provider'],AUTHOR_ROUTER_PROVIDER)
            self.assertEqual(routes['routes'],[])
            self.assertNotIn('timing',routes)
            self.assertFalse((root/'flow/timing/cut-timing-paths.json').exists())
            self.assertEqual(report['author_cost_policy'],'author-pressure-v1')
            self.assertEqual(validate_multi_fpga_flow_bundle(root/'flow')['status'],'pass')

    def test_empty_cost_is_executed_and_incidence_without_timing_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'scarcity';root.mkdir()
            report,routes,_=self._flow(root,single=True,author_cost_policy='author-sll-pressure-v1')
            self.assertEqual(report['author_cost_policy'],routes['author_routing']['cost_policy'])
            self.assertEqual(routes['author_routing']['cost_policy'],'author-sll-pressure-v1')
            root=Path(tmp)/'incidence';root.mkdir()
            with self.assertRaisesRegex(ValueError,'requires normalized timing paths'):
                self._flow(root,single=True,timing_driven=True,author_order_policy='sta-incidence-v1')

    def test_empty_best_two_executes_both_checked_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);report,routes,_=self._flow(root,single=True,timing_driven=True,author_cost_policy='best-of-two-v1')
            selected=read_json(root/'flow/author-route-tdm/selection.json')
            self.assertEqual(report['author_cost_policy'],'best-of-two-v1')
            self.assertEqual([x['status'] for x in selected['candidates']],['pass','pass'])
            self.assertEqual(routes['provider'],AUTHOR_ROUTER_PROVIDER)
            self.assertEqual(validate_multi_fpga_flow_bundle(root/'flow')['status'],'pass')

    def test_no_timing_flag_rejects_sta_selection_policies(self):
        for options in [dict(author_cost_policy='best-of-two-v1'),dict(author_order_policy='sta-incidence-v1')]:
            with tempfile.TemporaryDirectory() as tmp, self.subTest(options=options):
                with self.assertRaisesRegex(EmuFlowError,'--no-timing-driven'):
                    self._flow(Path(tmp),**options)

    def test_untimed_native_with_cuts_is_rejected_without_implicit_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(EmuFlowError,'native-load-balanced-timing-evaluated-v1'):
                self._flow(Path(tmp),route_provider='native-load-balanced-v1',router=str(tlr_router()))

    def test_explicit_legacy_keeps_identity_and_no_author_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);report,routes,_=self._flow(root,route_provider=NATIVE_TIMING_EVALUATED_PROVIDER,router=str(tlr_router()))
            self.assertEqual(routes['provider'],NATIVE_TIMING_EVALUATED_PROVIDER)
            self.assertNotIn('author_cost_policy',report)
            self.assertNotIn('author_order_policy',report)
            self.assertEqual(validate_multi_fpga_flow_bundle(root/'flow')['status'],'pass')

    def test_author_worker_count_rejected_before_any_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'never'
            calls=[
                lambda:run_multi_fpga_flow(output_dir=out,route_candidate_workers=8),
                lambda:run_frame_length_search(root/'a',root/'b',None,out,root/'r',root/'t',max_frame_slots=32,candidate_workers=8),
                lambda:run_cross_stage_optimization(ir_path=root/'a',platform_path=root/'b',database_path=root/'d',initial_assignment_path=root/'p',output_dir=out,route_candidate_workers=8),
            ]
            for invoke in calls:
                with self.subTest(invoke=invoke),self.assertRaisesRegex(EmuFlowError,'workers 1'):
                    invoke()
                self.assertFalse(out.exists())

    def test_old_bounded_author_summary_replays_and_wrong_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);report,_,_=self._flow(root)
            report['stages']['system_route'].pop('author_routing')
            write_json(root/'flow/multi-fpga-flow-report.json',report)
            self.assertEqual(validate_multi_fpga_flow_bundle(root/'flow')['status'],'pass')
            report['author_cost_policy']='author-sll-pressure-v1'
            write_json(root/'flow/multi-fpga-flow-report.json',report)
            with self.assertRaisesRegex(ValidationError,'cost policy disagrees'):
                validate_multi_fpga_flow_bundle(root/'flow')

    def test_frame_seals_actual_defaults_and_reads_legacy_v4(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);assignment,platform,timing,_=mode_fixtures.AuthorFlowModesTest()._inputs(root)
            report=run_frame_length_search(assignment,platform,timing,root/'search',root/'routes',root/'tdm',
                max_frame_slots=16,route_provider=None,author_cost_policy=None,author_order_policy=None,
                router=author_router(),tdm_provider=TDM_BASELINE_PROVIDER,simulation_frames=2)
            expected=dict(route_provider=AUTHOR_ROUTER_PROVIDER,author_cost_policy='author-pressure-v1',
                          author_order_policy='hard-connections-v1',tdm_provider=TDM_BASELINE_PROVIDER)
            for key,value in expected.items():self.assertEqual(report['configuration'][key],value)
            try:
                from jsonschema import Draft202012Validator
            except ImportError:
                pass
            else:
                Draft202012Validator(read_json(ROOT/'schemas/frame-search-v5.schema.json')).validate(report)
            unknown=copy.deepcopy(report)
            unknown['configuration']['route_provider']='unknown'
            for key in ['author_cost_policy','author_order_policy']:unknown['configuration'].pop(key)
            with self.assertRaises(ValidationError):validate_frame_search_report(unknown)
            legacy=copy.deepcopy(report);legacy['schema']='emuflow.frame-search/v4'
            for key in ['route_provider','author_cost_policy','author_order_policy']:legacy['configuration'].pop(key)
            self.assertEqual(validate_frame_search_report(legacy)['status'],'pass')
            legacy['configuration'].update(route_provider=None,author_cost_policy='author-pressure-v1')
            with self.assertRaises(ValidationError):validate_frame_search_report(legacy)
