import unittest
from emuflow.ecp5_data_graph import build_ecp5_data_graph
from emuflow.errors import ValidationError
import test_ecp5_timing_coverage as fixture


class DataGraphTests(unittest.TestCase):
    def test_register_feedback_stops_at_sequential_boundary(self):
        r, d = fixture.Ecp5TimingCoverageTests().model()
        g = build_ecp5_data_graph(r, d)
        self.assertEqual(set(g['roots']), {('ff', 'Q')})
        self.assertEqual(set(g['captures']), {('ff', 'M')})
        self.assertEqual([(a, b) for a, b, _ in g['edges']], [(('ff', 'Q'), ('ff', 'M'))])
        self.assertFalse(g['global_timing_qualified'])

    def test_constant_capture_is_not_an_invented_timing_path(self):
        r, d = fixture.Ecp5TimingCoverageTests().model()
        r['modules']['top']['cells']['ff']['connections']['M'] = ['0']
        del d['interconnect'][('ff','Q'),('ff','M')]
        g = build_ecp5_data_graph(r, d)
        self.assertIn(('ff','M'), g['constants'])
        self.assertNotIn(('ff','M'), g['dynamic_nodes'])

    def test_unknown_hard_block_is_not_an_implicit_zero_delay(self):
        r, d = fixture.Ecp5TimingCoverageTests().model()
        r['modules']['top']['cells']['unknown'] = {'type':'MULT18X18D','connections':{}}
        with self.assertRaisesRegex(ValidationError, 'unsupported physical'):
            build_ecp5_data_graph(r, d)

    def test_combinational_cycle_is_rejected(self):
        r, d = fixture.Ecp5TimingCoverageTests().model(); cells = r['modules']['top']['cells']
        cells['loop'] = {'type':'TRELLIS_COMB','parameters':{'MODE':'LOGIC'},
            'connections':{'A':[8],'F':[8]},'port_directions':{'A':'input','F':'output'}}
        delay=((.1,.2,.3),(.1,.2,.3))
        d['interconnect'][('loop','F'),('loop','A')] = delay
        d['cells']['loop']={'iopaths':{('A','F'):delay}}
        with self.assertRaisesRegex(ValidationError, 'cycle'):
            build_ecp5_data_graph(r, d)
