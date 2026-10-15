"""Concrete bank-direction and per-hop ratio-delay contract regressions."""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.phase5 import run_phase5, validate_phase5
from emuflow.routing import normalize_route_constraints
from emuflow.tdm import (
    _bank_direction_allocations, build_tdm_schedule,
    reconstruct_tdm_schedule_timing, reconstruct_tdm_schedule_timing_paths,
    reconstruct_tdm_schedule_timing_paths_from_routes,
    schedule_to_systemverilog_testbench, tdm_hop_latency_cycles,
    validate_tdm_schedule,
)
from emuflow.tdm_ratio import _prepare_model, build_tdm_ratio_plan, validate_tdm_ratio_plan
from emuflow.tdm_slot import refine_tdm_schedule_native
from emuflow.tdm_oracle import exact_multi_round_slot_schedule
from emuflow.tdm_timing_dag import build_timing_dag_ratio_plan
from tests.native_build import tdm_ratio_optimizer, tdm_slot_optimizer, tdm_timing_dag_optimizer
from tests.test_phase5 import _platform_value, _link

ROOT = Path(__file__).resolve().parents[1]


def platform_and_routes(forward=1, reverse=0, *, chain=False, minimum=1):
    links = [_link('ab', 'a', 'b', lanes=360, latency=2)]
    if chain:
        links.append(_link('bc', 'b', 'c', lanes=360, latency=2))
    for link in links:
        link.update(capacity_sharing='shared_bidirectional', bank_count=18,
                    wires_per_bank=20,
                    tdm_delay_model={'model':'linear-ratio-v1','intercept_ns':30.0,
                                     'ratio_coefficient_ns':0.5})
    platform = Platform.from_dict(_platform_value('bank_ratio', ['a','b','c'] if chain else ['a','b'], links))
    records = []
    for i in range(forward + reverse):
        src, dst = ('a','b') if i < forward else ('b','a')
        edges = [{'link':'ab','from':src,'to':dst}]
        if chain:
            edges.append({'link':'bc','from':'b','to':'c'})
            dst = 'c'
        records.append({'id':f'd{i:04d}','net':f'n{i:04d}','source':src,
                        'sinks':[dst], 'tree_edges':edges})
    constraints = normalize_route_constraints({'schema':'emuflow.system-route-constraints/v1','frame_slots':64,'tdm_min_ratio':minimum}, platform)
    routes = {'schema':'emuflow.system-routes/v1','design':'bank_ratio',
              'platform':platform.name,'constraints':constraints,'routes':records,
              'timing':{'normalization':{'positive_slack_scale_ns':1.,'negative_slack_scale_ns':1.,'max_clock_period_ns':200.},
                        'paths':[{'path':f'p{i}','clock_domain':'clk','clock_period_ns':200.,'fixed_delay_ns':6.5,
                                  'cut_nets':[r['net']], 'cut_transitions':[{'net':r['net'],'from':r['source'],'to':r['sinks'][0]}]}
                                 for i,r in enumerate(records)]}}
    return platform, routes


def ratio_plan(platform, routes, **kw):
    return build_tdm_ratio_plan(routes, platform, executable=str(tdm_ratio_optimizer()),
                                max_iterations=20, post_refinement_iterations=0,
                                exact_domain_limit=0, **kw)


class BankRatioModelTest(unittest.TestCase):
    def test_asymmetric_bank_budget_is_integer_and_cable_can_mix_directions(self):
        platform, routes = platform_and_routes(80,280)
        allocation = _bank_direction_allocations(routes,platform,routes['constraints'])
        self.assertEqual(allocation[('ab','a','b')]['bank_count'],4)
        self.assertEqual(allocation[('ab','b','a')]['bank_count'],14)
        schedule = build_tdm_schedule(routes,platform)
        self.assertEqual(validate_tdm_schedule(routes,platform,schedule)['status'],'pass')
        directions = {}
        for e in schedule['entries']:
            bank = e['lane'] // 20
            directions.setdefault(bank,set()).add((e['from'],e['to']))
        self.assertEqual(len(directions),18)
        self.assertTrue(all(len(d)==1 for d in directions.values()))
        # cable 2 consists of bank indexes 3,4,5 and may contain both directions.
        self.assertNotEqual(directions[3], directions[4])
        corrupt = copy.deepcopy(schedule)
        e = next(e for e in corrupt['entries'] if e['from']=='b')
        e['lane']=0
        e['slot']+=1  # Opposite directions cannot reuse the bank even at different slots.
        with self.assertRaisesRegex(ValidationError,'allocated direction banks'):
            validate_tdm_schedule(routes,platform,corrupt)

    def test_native_ratio_plan_cannot_treat_360_wires_as_fractional_banks(self):
        platform,routes = platform_and_routes(181,179)
        plan = ratio_plan(platform,routes)
        validate_tdm_ratio_plan(routes,platform,plan)
        self.assertEqual(sum(d['bank_count'] for d in plan['domains']),18)
        self.assertTrue(all(d['lanes'] % 20 == 0 and d['lane_offset'] % 20 == 0 for d in plan['domains']))
        self.assertGreater(max(h['discrete_ratio'] for h in plan['hops']),1)
        schedule=build_tdm_schedule(routes,platform,plan)
        validate_tdm_schedule(routes,platform,schedule,plan)
        corrupt=copy.deepcopy(plan)
        reverse=next(h for h in corrupt['hops'] if h['from']=='b')
        reverse['lane']=0
        with self.assertRaisesRegex(ValidationError,'lane is invalid'):
            validate_tdm_ratio_plan(routes,platform,corrupt)

    def test_per_hop_8_and_16_sum_without_slot_wait_and_keep_clock(self):
        platform,routes=platform_and_routes(chain=True,minimum=8)
        schedule=build_tdm_schedule(routes,platform)
        first,second=sorted(schedule['entries'],key=lambda e:e['hop'])
        self.assertEqual(first['arrival_slot']-first['slot'],9)
        self.assertGreaterEqual(second['slot'],first['arrival_slot']+1)
        # Reconstruction is also used on externally supplied, already qualified
        # assignments, so directly exercise two different per-hop ratios.
        second['tdm_ratio']=16
        second['arrival_slot']=second['slot']+10
        paths=reconstruct_tdm_schedule_timing_paths_from_routes(routes,platform,schedule)
        self.assertEqual(paths[0]['transport_delay_ns'],72.)
        self.assertEqual(paths[0]['delay_ns'],78.5)
        self.assertEqual([h['link_tdm_delay_ns'] for h in paths[0]['scheduled_hops']],[34.,38.])
        self.assertEqual([h['tdm_slot_ns'] for h in paths[0]['scheduled_hops']],[4.,4.])
        self.assertEqual([h['tdm_ratio'] for h in paths[0]['scheduled_hops']],[8,16])
        self.assertEqual(tdm_hop_latency_cycles(platform.links[0],8),9)
        self.assertEqual(tdm_hop_latency_cycles(platform.links[1],16),10)
        self.assertEqual(reconstruct_tdm_schedule_timing_paths(routes,platform,schedule),paths)
        corrupt=copy.deepcopy(schedule);corrupt['entries'][0]['tdm_ratio']=0
        with self.assertRaisesRegex(ValidationError,'tdm_ratio'):
            validate_tdm_schedule(routes,platform,corrupt)

    def test_native_slot_and_oracle_match_ratio_objective_and_dynamic_arrival(self):
        platform,routes=platform_and_routes(chain=True,minimum=8)
        plan=ratio_plan(platform,routes)
        self.assertTrue(all(h['beta_ns']==.5 and h['slot_period_ns']==4 for h in plan['hops']))
        # Qualify an explicit heterogeneous ratio assignment before exercising
        # the independent native scheduler. The expected arithmetic is fixed.
        next(h for h in plan['hops'] if h['link']=='bc')['discrete_ratio']=16
        plan['timing_paths'][0].update(delay_ns=78.5,slack_ns=121.5,normalized_slack=121.5)
        plan['metrics']['max_discrete_ratio']=16
        plan['metrics']['discrete_worst_normalized_slack']=121.5
        validate_tdm_ratio_plan(routes,platform,plan)
        schedule=build_tdm_schedule(routes,platform,plan)
        refined=refine_tdm_schedule_native(routes,platform,plan,schedule,
                                          executable=str(tdm_slot_optimizer()),max_iterations=10)
        oracle=exact_multi_round_slot_schedule(routes,platform,plan,max_hops=2)
        timing=reconstruct_tdm_schedule_timing(routes,platform,refined)
        self.assertEqual(timing['timing_model'],'per-hop-ratio-delay-v1')
        self.assertAlmostEqual(timing['worst_normalized_slack'],oracle['worst_normalized_slack'])
        self.assertEqual(timing['worst_delay_ns'],78.5)  # 34 + 38 + 6.5 local
        self.assertEqual(sorted(e['arrival_slot']-e['slot'] for e in refined['entries']),[9,10])
        # Slot positions remain causally constrained but cannot change d(R).
        self.assertGreater(max(e['slot'] for e in refined['entries']),0)

    def test_timing_dag_uses_ratio_coefficient_and_concrete_banks(self):
        platform,routes=platform_and_routes(3,2,minimum=8)
        plan=build_timing_dag_ratio_plan(routes,platform,
            dag_executable=str(tdm_timing_dag_optimizer()),legalization_executable=str(tdm_ratio_optimizer()),
            max_iterations=20,post_refinement_iterations=0,exact_domain_limit=0)
        schedule=build_tdm_schedule(routes,platform,plan)
        validate_tdm_schedule(routes,platform,schedule,plan)
        self.assertTrue(all(h['beta_ns']==.5 for h in plan['hops']))
        self.assertEqual(reconstruct_tdm_schedule_timing(routes,platform,schedule)['worst_delay_ns'],40.5)

    def test_worst_multicast_sink_uses_scheduled_ratio_not_r1_delay(self):
        platform,routes=platform_and_routes(chain=True,minimum=8)
        value=platform.to_dict()
        value['links'][1]['endpoints']=['a','c']
        platform=Platform.from_dict(value)
        routes['routes'][0]['tree_edges'][1]['from']='a'
        routes['routes'][0]['sinks']=['b','c']
        del routes['timing']['paths'][0]['cut_transitions']
        schedule=build_tdm_schedule(routes,platform)
        c_entry=next(e for e in schedule['entries'] if e['to']=='c')
        c_entry['tdm_ratio']=16
        c_entry['arrival_slot']=c_entry['slot']+10
        result=reconstruct_tdm_schedule_timing_paths_from_routes(routes,platform,schedule)[0]
        self.assertEqual(result['cut_transitions'][0]['to'],'c')
        self.assertEqual(result['transport_delay_ns'],38.)
        self.assertEqual(reconstruct_tdm_schedule_timing_paths(routes,platform,schedule)[0],result)

    def test_phase5_persistence_and_independent_validation(self):
        platform,routes=platform_and_routes(chain=True,minimum=8)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'routes.json').write_text(json.dumps(routes))
            (root/'platform.json').write_text(json.dumps(platform.to_dict()))
            report=run_phase5(root/'routes.json',root/'platform.json',root/'phase5',
                ratio_optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                slot_optimizer=str(tdm_slot_optimizer()),ratio_max_iterations=20,
                post_refinement_iterations=0,slot_refinement_iterations=1,
                simulation_frames=2)
            self.assertEqual(report['status'],'pass')
            validation=validate_phase5(root/'routes.json',root/'platform.json',
                root/'phase5/schedule.json',root/'phase5/ratio_plan.json')
            self.assertEqual(validation['status'],'pass')
            persisted=json.loads((root/'phase5/schedule.json').read_text())
            self.assertTrue(persisted['bank_allocations'])
            self.assertTrue(all(e['tdm_ratio']==8 for e in persisted['entries']))

    @unittest.skipUnless(shutil.which('iverilog') and shutil.which('vvp'),'iverilog is not installed')
    def test_transport_rtl_testbench_uses_schedule_latency(self):
        platform,routes=platform_and_routes(chain=True,minimum=8)
        schedule=build_tdm_schedule(routes,platform)
        text=schedule_to_systemverilog_testbench(routes,schedule,platform,frames=3)
        self.assertIn('.LATENCY(9)',text)
        self.assertNotIn('.LATENCY(2)',text)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'tb.sv').write_text(text)
            subprocess.run(['iverilog','-g2012','-s','transport_schedule_tb','-o',str(root/'sim'),
                            str(ROOT/'rtl/transport/emuflow_tdm_link.sv'),str(root/'tb.sv')],check=True,capture_output=True)
            subprocess.run(['vvp',str(root/'sim')],check=True,capture_output=True)
