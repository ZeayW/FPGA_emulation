import unittest
from emuflow.ir import EmuIR
from emuflow.snapshot_rounds import derive_snapshot_rounds
from emuflow.errors import ValidationError


def chain():
    def ep(i,p): return dict(instance=i,port=p,bit=0)
    cells=[dict(id="q",type="$_DFF_P_",resources={"ff":1})]
    cells += [dict(id=i,type="LUT1",parameters={"INIT":"01"},resources={"lut":1}) for i in ("x","y")]
    nets=[]
    for n,a,p,b,r in (("first","q","Q","x","I0"),("second","x","O","y","I0"),("third","y","O","q","D")):
        nets.append(dict(id=n,cut_class="combinational",drivers=[ep(a,p)],sinks=[ep(b,r)]))
    return EmuIR(dict(schema="emuflow.emuir/v1",design=dict(name="chain",top="chain",source_format="test"),
                      ports=[],clocks=[],instances=cells,nets=nets))


class SnapshotRoundTests(unittest.TestCase):
    def test_crossing_depth_not_gate_count(self):
        ir=chain()
        # q on A -> x on B -> y on A -> q on A: two crossings.
        self.assertEqual(derive_snapshot_rounds(ir,{"q":"board0","x":"board1","y":"board0"},port_owners={}),2)
        self.assertEqual(derive_snapshot_rounds(ir,{i:"board0" for i in ("q","x","y")},port_owners={}),1)

    def test_register_feedback_is_not_combinational_cycle(self):
        self.assertEqual(derive_snapshot_rounds(chain(),{"q":"board0","x":"board1","y":"board1"},port_owners={}),2)

    def test_combinational_loop_rejected(self):
        ir=chain(); ir.value["nets"][0]["drivers"][0]=dict(instance="y",port="O",bit=0)
        with self.assertRaises(ValidationError):
            derive_snapshot_rounds(ir,{"q":"board0","x":"board1","y":"board0"},port_owners={})

    def test_third_crossing_to_output_owner(self):
        ir=chain(); ir.value["ports"].append(dict(id="out",direction="output",width=1))
        ir.value["nets"][-1]["sinks"].append(dict(instance=None,port="out",bit=0))
        self.assertEqual(derive_snapshot_rounds(ir,{"q":"board0","x":"board1","y":"board0"},port_owners={"out":"board1"}),3)
