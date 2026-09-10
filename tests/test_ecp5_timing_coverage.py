import unittest
from emuflow.ecp5_timing_coverage import qualify_ecp5_timing_coverage
from emuflow.errors import ValidationError


class Ecp5TimingCoverageTests(unittest.TestCase):
    def model(self):
        r={'modules':{'top':{'ports':{},'cells':{
            'clock':{'type':'DCCA','port_directions':{'CLKO':'output'},'connections':{'CLKO':[1]}},
            'ff':{'type':'TRELLIS_FF','parameters':{'SD':'0 ','CLKMUX':'CLK','CEMUX':'1 ','SRMODE':'LSR_OVER_CE'},
                  'port_directions':{'CLK':'input','M':'input','Q':'output'},'connections':{'CLK':[1],'M':[2],'Q':[2]}}
        }}}}
        triple=((.1,.2,.3),(.1,.2,.3))
        d={'delay_connectivity_checked':True,'interconnect':{
            (('clock','CLKO'),('ff','CLK')):triple,(('ff','Q'),('ff','M')):triple},
            'cells':{'ff':{'iopaths':{('CLK','Q'):triple},'setuphold':{
                ((edge,'M'),('posedge','CLK')):triple for edge in ('posedge','negedge')}}}}
        return r,d

    def test_complete_annotations_not_global_sta(self):
        r,d=self.model();result=qualify_ecp5_timing_coverage(r,d)
        self.assertEqual(result['routed_connections'],2)
        self.assertEqual(result['ff_checked_inputs'],1)
        self.assertFalse(result['global_timing_qualified'])
        self.assertFalse(result['primitive_combinational_arc_coverage_qualified'])

    def test_route_missing_reversed_extra_or_undriven_fail(self):
        for mutation in ('missing','reverse','extra','undriven','direction'):
            r,d=self.model();w=d['interconnect'];key=(('ff','Q'),('ff','M'))
            if mutation=='missing':del w[key]
            if mutation=='reverse':w[tuple(reversed(key))]=w.pop(key)
            if mutation=='extra':w[(('ff','Q'),('ff','CLK'))]=w[key]
            if mutation=='undriven':r['modules']['top']['cells']['ff']['connections']['M']=[9]
            if mutation=='direction':del r['modules']['top']['cells']['ff']['port_directions']['M']
            with self.subTest(mutation=mutation),self.assertRaises(ValidationError):qualify_ecp5_timing_coverage(r,d)

    def test_missing_clock_to_q_or_one_transition_fails(self):
        for mutation in ('cq','fall','rise'):
            r,d=self.model();a=d['cells']['ff']
            if mutation=='cq':a['iopaths'].clear()
            else:del a['setuphold'][(('posedge' if mutation=='rise' else 'negedge','M'),('posedge','CLK'))]
            with self.subTest(mutation=mutation),self.assertRaises(ValidationError):qualify_ecp5_timing_coverage(r,d)

    def test_asynchronous_reset_is_explicitly_unqualified(self):
        r,d=self.model();r['modules']['top']['cells']['ff']['parameters']['SRMODE']='ASYNC'
        result=qualify_ecp5_timing_coverage(r,d)
        self.assertEqual(result['asynchronous_assertion_pins'],[('ff','LSR')])
        self.assertFalse(result['recovery_removal_qualified'])

    def test_omitted_mode_only_allowed_without_reset_port(self):
        r,d=self.model();ff=r['modules']['top']['cells']['ff']
        del ff['parameters']['SRMODE']
        self.assertEqual(qualify_ecp5_timing_coverage(r,d)['ff_checked_inputs'],1)
        ff['connections']['LSR']=['0'];ff['port_directions']['LSR']='input'
        with self.assertRaises(ValidationError):qualify_ecp5_timing_coverage(r,d)
