import copy
import unittest
from emuflow.errors import ValidationError
from emuflow.snapshot_cdc import qualify_snapshot_uart_cdc


class SnapshotCdcTests(unittest.TestCase):
    def fixture(self):
        params={"CLKMUX":"CLK","CEMUX":"1 ","SD":"0 ","SRMODE":"LSR_OVER_CE","REGSET":"SET","LSRMUX":"LSR"}
        def ff(d,q):
            return {"type":"TRELLIS_FF","parameters":dict(params),
                "connections":{"M":[d],"Q":[q],"CLK":[9],"LSR":[8]},
                "port_directions":{"M":"input","Q":"output","CLK":"input","LSR":"input"}}
        routed={"modules":{"top":{"ports":{"link_rx":{"direction":"input","bits":[1]}},
            "netnames":{"core.endpoint.phy.rx_meta":{"bits":[3]},"core.endpoint.phy.rx_sync":{"bits":[4]}},
            "cells":{"meta":ff(2,3),"sync":ff(3,4),
                "pad":{"type":"TRELLIS_IO","parameters":{"DIR":"INPUT"},"connections":{"B":[1],"O":[2]},"port_directions":{"B":"inout","O":"output"}}}}}}
        triple=((.1,.2,.3),(.2,.3,.4))
        delays={"delay_connectivity_checked":True,"interconnect":{(("meta","Q"),("sync","M")):triple},
            "cells":{"meta":{"iopaths":{("CLK","Q"):triple}},"sync":{"setuphold":{
                ((edge,"M"),("posedge","CLK")):triple for edge in ("posedge","negedge")}}}}
        return routed,{"modules":{"device":{"netnames":{}}}},delays

    def test_direct_two_ff_chain_with_routed_annotation(self):
        r,m,d=self.fixture()
        result=qualify_snapshot_uart_cdc(r,m,d,mapped_top="device")
        self.assertEqual(result['receivers']['link_rx']['meta_to_sync_wire_max_ns'],.4)
        self.assertEqual(result['receivers']['link_rx']['sync_setup_max_ns'],.3)
        self.assertFalse(result['metastability_mtbf_qualified'])
        self.assertFalse(result['reset_cdc_qualified'])

    def test_bypass_fanout_clocks_or_gate_fail(self):
        for mutation in ('bypass','fanout','clock','enable','reset','merged','missing_direction','pad'):
            r,m,d=self.fixture();module=r['modules']['top'];cells=module['cells']
            if mutation=='bypass':cells['sync']['connections']['M']=[2]
            if mutation=='fanout':cells['consumer']=copy.deepcopy(cells['sync'])
            if mutation=='clock':cells['sync']['connections']['CLK']=[99]
            if mutation=='enable':cells['sync']['parameters']['CEMUX']='CE'
            if mutation=='reset':cells['sync']['parameters']['SRMODE']='ASYNC'
            if mutation=='merged':module['netnames']['core.endpoint.phy.rx_sync']['bits']=[3]
            if mutation=='missing_direction':del cells['sync']['port_directions']['M']
            if mutation=='pad':cells['pad']['connections']['B']=[99]
            with self.subTest(mutation=mutation),self.assertRaises(ValidationError):
                qualify_snapshot_uart_cdc(r,m,d,mapped_top='device')

    def test_missing_delays_or_host_receiver_fail(self):
        for mutation in ('wire','cq','setup','unchecked','host'):
            r,m,d=self.fixture()
            if mutation=='wire':d['interconnect'].clear()
            if mutation=='cq':d['cells']['meta']['iopaths'].clear()
            if mutation=='setup':d['cells']['sync']['setuphold'].clear()
            if mutation=='unchecked':d['delay_connectivity_checked']=False
            with self.subTest(mutation=mutation),self.assertRaises(ValidationError):
                qualify_snapshot_uart_cdc(r,m,d,mapped_top='device',host_uart=mutation=='host')
