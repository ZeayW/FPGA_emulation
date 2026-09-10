import unittest
from emuflow.ir import EmuIR
from emuflow.errors import ValidationError
from emuflow.snapshot_netlist import emit_snapshot_partition


def fixture():
    def ep(i,p): return dict(instance=i,port=p,bit=0)
    def net(n,c,d,s): return dict(id=n,cut_class=c,drivers=[ep(*d)],sinks=[ep(*v) for v in s])
    return EmuIR(dict(schema="emuflow.emuir/v1",
        design=dict(name="miter",top="miter",source_format="qualification"),clocks=[],
        ports=[dict(id="clk",width=1,direction="input"),dict(id="result",width=1,direction="output")],
        instances=[dict(id=i,type="$_DFF_P_",resources={"ff":1}) for i in ("a","b")]+
                  [dict(id="inv",type="LUT1",resources={"lut":1},parameters={"INIT":"01"})],
        nets=[net("clk","clock",(None,"clk"),[("a","C"),("b","C")]),
              net("qa","register_output",("a","Q"),[("b","D"),(None,"result")]),
              net("qb","register_output",("b","Q"),[("inv","I0")]),
              net("comb","combinational",("inv","O"),[("a","D")])]))


class SnapshotNetlistTests(unittest.TestCase):
    def emit(self, board="board0", **kw):
        args=dict(board=board,module="partition_"+board,port_owners={"result":"board0"},initial_state={"a":0,"b":0})
        args.update(kw)
        return emit_snapshot_partition(fixture(),{"a":"board0","b":"board1","inv":"board1"},**args)

    def test_partition_uses_original_crossings(self):
        a,ra=self.emit(); b,rb=self.emit("board1")
        self.assertEqual(ra["imported_nets"],["comb"])
        self.assertEqual(ra["exported_nets"],["qa"])
        self.assertEqual(rb["exported_nets"],ra["imported_nets"])
        self.assertEqual(rb["imported_nets"],ra["exported_nets"])
        self.assertEqual((ra["local_instances"],rb["local_instances"]),(1,2))
        self.assertIn("else if(step)",a)
        self.assertIn("2'h1",b)
        self.assertEqual(ra["host_outputs"],[dict(port="result",bit=0,index=0)])

    def test_initial_state_not_invented(self):
        for state in ({},{"a":0},{"a":0,"b":True}):
            with self.assertRaises(ValidationError): self.emit(initial_state=state)

    def test_clock_and_reset_are_not_silently_changed(self):
        ir=fixture(); ir.value["nets"][0]["cut_class"]="reset"
        with self.assertRaises(ValidationError):
            emit_snapshot_partition(ir,{"a":"board0","b":"board1","inv":"board1"},
                board="board0",module="partition",port_owners={"result":"board0"},initial_state={"a":0,"b":0})

    def test_no_source_mutation(self):
        ir=fixture(); import copy; before=copy.deepcopy(ir.value)
        emit_snapshot_partition(ir,{"a":"board0","b":"board1","inv":"board1"},
            board="board0",module="partition",port_owners={"result":"board0"},initial_state={"a":0,"b":0})
        self.assertEqual(before,ir.value)
