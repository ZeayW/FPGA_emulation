import unittest
from emuflow.ecp5_data_graph import build_ecp5_data_graph, project_data_reachability, require_data_connections
from emuflow.errors import ValidationError
import test_ecp5_timing_coverage as fixture


class DataGraphTests(unittest.TestCase):
    def test_reachability_preserves_original_aliases_and_rejects_missing_pairs(self):
        from test_ecp5_sta import graph
        g=graph()
        projection=project_data_reachability(g,{'original-a':('a','Q'),'merged-a':('a','Q'),'b':('b','Q')},
                                             {'capture':('sink','DI')})
        self.assertEqual(projection['capture_masks']['capture'],7)
        self.assertEqual(require_data_connections(projection,[('original-a','capture'),('merged-a','capture')])['required_pairs'],2)
        g['edges']=g['edges'][1:]
        projection=project_data_reachability(g,{'a':('a','Q'),'b':('b','Q')},{'sink':('sink','DI')})
        with self.assertRaisesRegex(ValidationError,'missing required'):
            require_data_connections(projection,[('a','sink')])
        with self.assertRaisesRegex(ValidationError,'unbound'):
            require_data_connections(projection,[('unknown','sink')])

    def test_reachability_requires_real_boundaries_and_topological_order(self):
        from test_ecp5_sta import graph
        g=graph()
        with self.assertRaisesRegex(ValidationError,'physical root'):
            project_data_reachability(g,{'a':('lut','F')},{'sink':('sink','DI')})
        g['order'].reverse()
        with self.assertRaisesRegex(ValidationError,'topologically'):
            project_data_reachability(g,{'a':('a','Q')},{'sink':('sink','DI')})

    def test_reconvergent_projection_does_not_enumerate_exponential_paths(self):
        root=('root','Q'); previous=root; order=[root]; edges=[]
        # Eighty diamonds represent 2**80 paths but only 241 graph nodes.
        for index in range(80):
            a=(str(index),'A');b=(str(index),'B');merge=(str(index),'Y')
            order.extend((a,b,merge))
            edges.extend((x,y,None) for x,y in ((previous,a),(previous,b),(a,merge),(b,merge)))
            previous=merge
        g={'roots':{root:{}},'captures':{previous:{}},'edges':edges,'order':order}
        result=project_data_reachability(g,{'original':root},{'capture':previous})
        self.assertEqual(result['capture_masks'],{'capture':1})

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
