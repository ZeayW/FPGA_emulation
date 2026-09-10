import unittest
from emuflow.snapshot_timing_population import build_snapshot_timing_population, boundary_id, required_snapshot_connections
from emuflow.errors import ValidationError
from test_snapshot_netlist import fixture


class SourcePopulationTests(unittest.TestCase):
    def build(self,board='board0',assignment=None,ir=None):
        return build_snapshot_timing_population(ir or fixture(),assignment or {'a':'board0','b':'board1','inv':'board1'},
            board=board,port_owners={'result':'board0'})

    def test_source_crossing_boundaries_include_local_and_host_paths(self):
        p=self.build();pairs=set(required_snapshot_connections(p));b=boundary_id
        self.assertEqual(pairs,{(b('state','a'),b('cut','qa')),
            (b('state','a'),b('host','result')),(b('cut','comb'),b('state','a'))})
        self.assertFalse(p['global_timing_qualified'])
        p=self.build('board1')
        self.assertEqual(set(required_snapshot_connections(p)),{
            (b('cut','qa'),b('state','b')),(b('state','b'),b('cut','comb'))})

    def test_same_board_logic_not_dropped(self):
        p=self.build(assignment={i:'board0' for i in ('a','b','inv')});b=boundary_id
        self.assertEqual(set(required_snapshot_connections(p)),{
            (b('state','a'),b('state','b')),(b('state','b'),b('state','a')),
            (b('state','a'),b('host','result'))})

    def test_constants_are_not_fabricated_source_paths(self):
        ir=fixture();ir.value['nets'][-1]['sinks']=[]
        ir.value['instances'][0]['constant_connections']=[{'port':'D','bit':0,'value':'0'}]
        p=self.build(ir=ir)
        self.assertEqual(p['capture_masks'][boundary_id('state','a')],0)

    def test_missing_input_and_unbound_reset_fail(self):
        ir=fixture();ir.value['nets'][-1]['sinks']=[]
        with self.assertRaisesRegex(ValidationError,'missing original FF'):self.build(ir=ir)
        ir=fixture();ir.value['nets'][0]['cut_class']='reset'
        with self.assertRaisesRegex(ValidationError,'reset'):self.build(ir=ir)
