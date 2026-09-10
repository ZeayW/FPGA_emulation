import unittest
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.snapshot_pair import emit_snapshot_pair


def host_fixture():
    def ep(i,p): return dict(instance=i,port=p,bit=0)
    def net(n,c,d,s): return dict(id=n,cut_class=c,drivers=[ep(*d)],sinks=[ep(*v) for v in s])
    ir=EmuIR(dict(schema="emuflow.emuir/v1",
        design=dict(name="host",top="host",source_format="qualification"),clocks=[],
        ports=[dict(id="clk",direction="input",width=1),
               dict(id="in",direction="input",width=1),dict(id="out",direction="output",width=1)],
        instances=[dict(id="q",type="$_DFF_P_",resources={"ff":1}),
                   dict(id="x",type="LUT2",parameters={"INIT":"0110"},resources={"lut":1})],
        nets=[net("clk","clock",(None,"clk"),[("q","C")]),
              net("input","primary_input",(None,"in"),[("x","I0")]),
              net("state","register_output",("q","Q"),[("x","I1"),(None,"out")]),
              net("next","combinational",("x","O"),[("q","D")])]))
    return ir, {"q":"board1","x":"board1"}


def generated_host_pair(**kwargs):
    ir,assignment=host_fixture()
    args=dict(prefix="host_pair",port_owners={"in":"board0","out":"board0"},
              initial_state={"q":0},session_id=0x789)
    args.update(kwargs)
    return emit_snapshot_pair(ir,assignment,**args)


class SnapshotPairTests(unittest.TestCase):
    def test_host_mapping_is_preserved(self):
        pair=generated_host_pair()
        self.assertEqual(pair["evaluation_rounds"],1)
        self.assertEqual(pair["output_sampling"],"pre_dut_active_edge")
        self.assertFalse(pair["physical_host_binding_qualified"])
        self.assertEqual(pair["boards"]["board0"]["interface"]["host_inputs"],
                         [dict(port="in",bit=0,index=0)])
        self.assertEqual(pair["boards"]["board1"]["interface"]["host_inputs"],[])
        self.assertIn("held_inputs<=host_inputs",pair["boards"]["board0"]["rtl"])

    def test_bad_host_and_session_contract_rejected(self):
        for args in (dict(port_owners={"in":"board1","out":"board0"}),
                     dict(port_owners={}),dict(session_id=True),dict(prefix="x;bad")):
            with self.subTest(args=args),self.assertRaises(ValidationError): generated_host_pair(**args)
