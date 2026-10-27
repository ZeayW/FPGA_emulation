"""Concrete bank-direction and per-hop ratio-delay contract regressions."""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.platform import Platform
from emuflow.phase5 import run_phase5, validate_phase5
from emuflow.routing import normalize_route_constraints
from emuflow.tdm import _bank_direction_allocations, schedule_to_systemverilog_testbench, tdm_hop_latency_cycles, validate_tdm_schedule
from tests.tdm_timing_oracle import timing_summary as reconstruct_tdm_schedule_timing, timing_paths as reconstruct_tdm_schedule_timing_paths, timing_paths as reconstruct_tdm_schedule_timing_paths_from_routes
from tests.schedule_reference import build_tdm_schedule
from tests.test_phase5 import _platform_value, _link

ROOT = Path(__file__).resolve().parents[1]


def platform_and_routes(forward=1, reverse=0, *, chain=False, minimum=8):
    links = [_link('ab', 'a', 'b', lanes=360, latency=2)]
    if chain:
        links.append(_link('bc', 'b', 'c', lanes=360, latency=2))
    for link in links:
        link.update(capacity_sharing='shared_bidirectional', bank_count=18,
                    wires_per_bank=20,
                    tdm_delay_model={'model':'linear-ratio-v1','intercept_ns':30.0,
                                     'ratio_coefficient_ns':0.5,
                                     'min_ratio':8,'max_ratio':64,
                                     'ratio_quantum':8})
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
        corrupt=copy.deepcopy(schedule);corrupt['entries'][0]['tdm_ratio']=264
        with self.assertRaisesRegex(ValidationError,'tdm_ratio'):
            validate_tdm_schedule(routes,platform,corrupt)


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
