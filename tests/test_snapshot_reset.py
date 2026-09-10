import copy
import unittest
from emuflow.errors import ValidationError
from emuflow.snapshot_reset import qualify_snapshot_reset_structure


class SnapshotResetTests(unittest.TestCase):
    def model(self):
        def ff(d,q):
            return {"type":"TRELLIS_FF","parameters":{"SD":"0 ","SRMODE":"ASYNC",
                "REGSET":"SET","LSRMUX":"LSR","CLKMUX":"CLK","CEMUX":"1 ","GSR":"DISABLED"},
                "connections":{"M":[d],"Q":[q],"CLK":[9],"LSR":[3]},
                "port_directions":{"M":"input","Q":"output","CLK":"input","LSR":"input"}}
        cells={"first":ff('0',4),"second":ff(4,5),
            "pad":{"type":"TRELLIS_IO","parameters":{"DIR":"INPUT"},"connections":{"B":[1],"O":[2]},"port_directions":{"B":"inout","O":"output"}},
            "inv":{"type":"TRELLIS_COMB","parameters":{"INITVAL":"0000000011111111"},"connections":{"D":[2],"F":[3]},"port_directions":{"D":"input","F":"output"}}}
        return {"modules":{"top":{"cells":cells,"ports":{"reset_n":{"direction":"input","bits":[1]}},
            "netnames":{"reset_pipe[0]":{"bits":[4]},"reset":{"bits":[5]}}}}}

    def test_structure_does_not_claim_analog_or_timing(self):
        result=qualify_snapshot_reset_structure(self.model())
        self.assertEqual(result['first_cell'],'first')
        self.assertFalse(result['recovery_removal_qualified'])
        self.assertFalse(result['global_timing_qualified'])

    def test_constant_lut_is_valid_and_nonconstant_is_not(self):
        r=self.model();c=r['modules']['top']['cells']
        c['first']['connections']['M']=[10]
        c['zero']={'type':'TRELLIS_COMB','parameters':{'INITVAL':'0'*16},'connections':{'F':[10]},'port_directions':{'F':'output'}}
        qualify_snapshot_reset_structure(r)
        c['zero']['parameters']['INITVAL']='1'*16
        with self.assertRaises(ValidationError):qualify_snapshot_reset_structure(r)

    def test_bypasses_polarity_gating_and_extra_async_state_fail(self):
        for kind in ('inverter','clock','assert','first_fanout','raw_fanout','gate','extra_async','merged','gsrcfg'):
            r=self.model();c=r['modules']['top']['cells']
            if kind=='inverter':c['inv']['parameters']['INITVAL']='1111111100000000'
            if kind=='clock':c['second']['connections']['CLK']=[8]
            if kind=='assert':c['second']['connections']['LSR']=[7]
            if kind=='first_fanout':c['inv']['connections']['A']=[4];c['inv']['port_directions']['A']='input'
            if kind=='raw_fanout':c['inv']['connections']['A']=[3];c['inv']['port_directions']['A']='input'
            if kind=='gate':c['first']['parameters']['CEMUX']='CE'
            if kind=='extra_async':c['extra']=copy.deepcopy(c['second']);c['extra']['connections']['Q']=[12]
            if kind=='merged':r['modules']['top']['netnames']['reset']['bits']=[4]
            if kind=='gsrcfg':c['first']['parameters']['GSR']='ENABLED'
            with self.subTest(kind=kind),self.assertRaises(ValidationError):qualify_snapshot_reset_structure(r)
