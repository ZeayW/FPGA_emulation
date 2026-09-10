import copy
import unittest
from emuflow.errors import ValidationError
from emuflow.snapshot_netlist import emit_snapshot_partition
from emuflow.snapshot_physical_binding import bind_snapshot_routed_identities
from test_snapshot_netlist import fixture


class SnapshotPhysicalBindingTests(unittest.TestCase):
    def test_source_binding_does_not_change_rtl(self):
        args=dict(board="board0",module="dut",port_owners={"result":"board0"},initial_state={"a":0,"b":0})
        assignment={"a":"board0","b":"board1","inv":"board1"}
        old, plain=emit_snapshot_partition(fixture(),assignment,**args)
        new, bound=emit_snapshot_partition(fixture(),assignment,include_source_binding=True,**args)
        self.assertEqual(old,new)
        self.assertNotIn("source_binding",plain)
        self.assertEqual(bound["source_binding"]["registers"],{"a":"state0"})
        self.assertEqual(set(bound["source_binding"]["nets"]),{"clk","qa","comb"})

    def model(self):
        source={"schema":"emuflow.snapshot-source-binding/v1","nets":{"n":"n0"},"registers":{"a":"s0","b":"s1","c":"s2"}}
        routed={"modules":{"top":{"netnames":{k:{"bits":[v]} for k,v in (("core.dut.n0",3),("core.dut.s0",3),("core.dut.s1",3),("core.dut.s2","0"))},"cells":{"actual":{"type":"TRELLIS_FF","connections":{"Q":[3]}}}}}}
        return source,routed

    def test_resolves_merged_state_and_explicit_constant(self):
        source,routed=self.model(); before=copy.deepcopy(routed)
        bound=bind_snapshot_routed_identities(source,routed,hierarchy="core.dut")
        self.assertEqual(bound["registers"]["a"]["cell"],bound["registers"]["b"]["cell"])
        self.assertEqual(bound["registers"]["c"]["kind"],"constant")
        self.assertFalse(bound["delay_annotation_qualified"])
        self.assertEqual(before,routed)

    def test_missing_unknown_or_ambiguous_fails(self):
        for mutation in ("missing","unknown","driver","multiple","wide"):
            source,routed=self.model(); m=routed["modules"]["top"]
            if mutation=="missing": del m["netnames"]["core.dut.s0"]
            if mutation=="unknown": m["netnames"]["core.dut.s0"]["bits"]=["x"]
            if mutation=="driver": m["cells"].clear()
            if mutation=="multiple": m["cells"]["other"]=copy.deepcopy(m["cells"]["actual"])
            if mutation=="wide": m["netnames"]["core.dut.s0"]["bits"]=[3,4]
            with self.subTest(mutation=mutation),self.assertRaises(ValidationError):
                bind_snapshot_routed_identities(source,routed,hierarchy="core.dut")

    def test_mapped_alias_bridge_uses_connectivity_not_numeric_id(self):
        source={"schema":"emuflow.snapshot-source-binding/v1","nets":{"n":"old"},"registers":{"a":"old"}}
        mapped={"modules":{"device":{"netnames":{"core.dut.old":{"bits":[42]},"canonical":{"bits":[41,42],"offset":7}}}}}
        routed={"modules":{"top":{"netnames":{"canonical[8]":{"bits":[90]}},"cells":{"ff":{"type":"TRELLIS_FF","connections":{"Q":[90]}}}}}}
        result=bind_snapshot_routed_identities(source,routed,hierarchy="core.dut",mapped=mapped,mapped_top="device")
        self.assertEqual(result["registers"]["a"]["bit"],90)
        routed["modules"]["top"]["netnames"]["core.dut.old"]={"bits":[42]}
        with self.assertRaises(ValidationError):
            bind_snapshot_routed_identities(source,routed,hierarchy="core.dut",mapped=mapped,mapped_top="device")
