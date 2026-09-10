import unittest
from emuflow.ulx3s_dut import build_ulx3s_snapshot_top, build_snapshot_boundary_plan
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR


class ULX3SDUTTests(unittest.TestCase):
    def build(self, **kw):
        args=dict(top="board_top",dut_module="partition",board="board0",
                  exported_bits=33,imported_bits=17,words=2,session_id=0x1234)
        args.update(kw)
        return build_ulx3s_snapshot_top(**args)

    def test_padding_and_role(self):
        result=self.build()
        self.assertIn("{31'b0, exported_values}",result)
        self.assertIn("remote_values[16:0]",result)
        self.assertIn(".LEADER(1)",result)
        self.assertIn(".LEADER(0)",self.build(board="board1"))
        self.assertIn(".step(commit && !fault && !link_fault && !reset)",result)

    def test_no_zero_width_concat(self):
        self.assertNotIn("0'b0",self.build(exported_bits=64))

    def test_invalid_contract(self):
        for kw in (dict(top="bad;name"),dict(dut_module="board_top"),
                   dict(board="board2"),dict(words=True),dict(words=257),
                   dict(exported_bits=65),dict(imported_bits=0),dict(session_id=-1)):
            with self.subTest(kw=kw), self.assertRaises(ValidationError): self.build(**kw)
        for rounds in (0,True,65536):
            with self.assertRaises(ValidationError): self.build(evaluation_rounds=rounds)
        self.assertIn(".ROUNDS(3)",self.build(evaluation_rounds=3))

    def test_real_ir_connectivity_does_not_filter_combination(self):
        def endpoint(instance, port): return dict(instance=instance,port=port,bit=0)
        value=dict(schema="emuflow.emuir/v1",design=dict(name="fixture",top="fixture",source_format="test"),
            clocks=[],ports=[dict(id="input",direction="input",width=1)],
            instances=[dict(id="a",type="LUT1",parameters={"INIT":"10"},resources={"lut":1}),
                       dict(id="b",type="LUT2",parameters={"INIT":"1000"},resources={"lut":1})],
            nets=[dict(id="comb",cut_class="combinational",drivers=[endpoint("a","O")],sinks=[endpoint("b","I0")]),
                  dict(id="pi",cut_class="primary_input",drivers=[endpoint(None,"input")],sinks=[endpoint("a","I0"),endpoint("b","I1")])])
        ir=EmuIR(value)
        assignment={"a":"board0","b":"board1"}
        plan=build_snapshot_boundary_plan(ir,assignment,port_owners={"input":"board0"})
        self.assertEqual(plan["outbound_nets"],{"board0":["comb","pi"],"board1":[]})
        self.assertFalse(plan["timing_and_evaluation_binding_qualified"])
        self.assertEqual(plan["words"],1)
        with self.assertRaises(ValidationError): build_snapshot_boundary_plan(ir,assignment,port_owners={})
        with self.assertRaises(ValidationError): build_snapshot_boundary_plan(ir,{"a":"board0"},port_owners={})
