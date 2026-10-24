import json
from pathlib import Path
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.xilinx_clock_leaf_domains import complete_ideal_ff_clock_leaf_loads
from emuflow.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index
from emuflow.yosys import import_yosys_json


class IdealClockLeafDomainTest(unittest.TestCase):
    def fixture(self, root, *, conflicting=False, unexpected=False):
        cells={"f":dict(type="FDRE",parameters={},attributes={},port_directions={"C":"input","D":"input","Q":"output"},connections={"C":[12 if unexpected else 10],"D":[2],"Q":[3]})}
        if conflicting:
            cells["g"]=dict(cells["f"],connections={"C":[11],"D":[4],"Q":[5]})
        source=root/"mapped.json";source.write_text(json.dumps({"modules":{"top":{"attributes":{"top":"1"},"ports":{},"cells":cells}}}))
        ir=root/"mapped.emuir.sqlite";import_yosys_json(source).save(ir)
        mapped=root/"mapped.sqlite";emit_xilinx_mapped_index(ir,mapped)
        with open_xilinx_mapped_index(mapped) as value:
            indexed=value["modules"]["top"]["cells"]
            first=indexed["f"]["connections"]["C"][0]
            second=indexed["g"]["connections"]["C"][0] if conflicting else first+1
        if unexpected:first+=1000
        route=root/"route.tsv";route.write_text("META\tpart\tfixture\n"+f"META\tmapped_sha256\t{file_sha256(mapped)}\n"+"CELL\tc0\tf\tFDRE\tSLICE_X1Y0\tAFF\n"+("CELL\tc1\tg\tFDRE\tSLICE_X1Y0\tBFF\n" if conflicting else "")+f"EXCLUDED\tn{first}\tboundary_clock\tideal-boundary-clock\nEXCLUDED\tn{second}\tboundary_clock\tideal-boundary-clock\n")
        payload=root/"domains.tsv";payload.write_text("D 0 1\nS SLICE_X0Y0 0 0\nS SLICE_X1Y0 0 0\nC SLICE_X0Y0 CLK1 n20\n")
        audit=root/"audit.json";audit.write_text(json.dumps(dict(schema="emuflow.clock-leaf-domain-audit/v1",clock_nets=1,input_sha256=file_sha256(route),source=dict(part="fixture",mapped_sha256=file_sha256(mapped)),placement_domain=dict(schema="emuflow.clock-leaf-placement-domain/v1",path=payload.name,sha256=file_sha256(payload),slice_clock_pins=1))))
        return audit,route,mapped,payload

    def test_reserves_declared_ideal_ff_loads_without_claiming_physical_routes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);audit,route,mapped,payload=self.fixture(root)
            before=payload.read_bytes()
            output=root/"complete.json"
            result=complete_ideal_ff_clock_leaf_loads(audit,route,mapped,output)
            clock=next(line.split()[1] for line in route.read_text().splitlines() if line.startswith("EXCLUDED"))
            self.assertEqual(result["ideal_boundary_ff_clock_nets"],[clock])
            self.assertEqual(result["ideal_boundary_ff_clock_pins"],1)
            self.assertEqual(result["physical_clock_nets"],1)
            self.assertEqual(result["oversubscribed_domains"],1)
            self.assertEqual(payload.read_bytes(),before)
            self.assertIn(f"C\tSLICE_X1Y0\tCLK1\t{clock}\n",(root/result["placement_domain"]["path"]).read_text())
            again=complete_ideal_ff_clock_leaf_loads(output,route,mapped,root/"again.json")
            self.assertEqual(again["ideal_boundary_ff_clock_pins"],1)

    def test_unknown_clock_or_conflicting_half_is_not_silently_idealized(self):
        for option in [dict(unexpected=True),dict(conflicting=True)]:
            with self.subTest(option=option),tempfile.TemporaryDirectory() as d:
                root=Path(d);audit,route,mapped,_=self.fixture(root,**option)
                with self.assertRaises(ValidationError):
                    complete_ideal_ff_clock_leaf_loads(audit,route,mapped,root/"complete.json")
                self.assertFalse((root/"complete.json").exists())

    def test_payload_tampering_and_source_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);audit,route,mapped,payload=self.fixture(root)
            with self.assertRaisesRegex(ValidationError,"separately"):
                complete_ideal_ff_clock_leaf_loads(audit,route,mapped,audit)
            payload.write_text(payload.read_text()+"\n")
            with self.assertRaisesRegex(ValidationError,"payload hash"):
                complete_ideal_ff_clock_leaf_loads(audit,route,mapped,root/"complete.json")


if __name__=="__main__":unittest.main()
