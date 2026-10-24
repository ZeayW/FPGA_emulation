import json
from pathlib import Path
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.xilinx_clock_leaf_domains import complete_ideal_ff_clock_leaf_loads, add_clock_region_policy
from tests.test_xilinx_openparf_atomic import _fixture
from emuflow.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index
from emuflow.xilinx_physical_handoff import write_xilinx_physical_handoff
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

    def test_region_policy_joins_source_architecture_and_keeps_original_payload(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);audit,route,mapped,payload=self.fixture(root)
            payload.write_text(payload.read_text().replace("D 0 1", "D 0 16"))
            doc=json.loads(audit.read_text());doc["placement_domain"]["sha256"]=file_sha256(payload);audit.write_text(json.dumps(doc))
            complete=root/"complete.json";complete_ideal_ff_clock_leaf_loads(audit,route,mapped,complete)
            arch_root=root/"arch";arch_root.mkdir();_,_,architecture=_fixture(arch_root,clock_regions=True)
            report=add_clock_region_policy(complete,route,architecture,root/"regional.json",maximum_clocks_per_region=1)
            self.assertEqual(report["over_budget_clock_regions"],1)
            self.assertEqual(report["oversubscribed_domains"],1)
            self.assertEqual(report["placement_domain"]["schema"],"emuflow.clock-leaf-placement-domain/v2")
            self.assertEqual(report["clock_region_policy"]["architecture_sha256"],file_sha256(architecture))
            text=(root/report["placement_domain"]["path"]).read_text()
            self.assertIn("R\tSLICE_X0Y0\t",text)
            self.assertIn("R\tSLICE_X1Y0\t",text)
            self.assertNotIn("R\t",payload.read_text())
            with self.assertRaisesRegex(ValidationError,"budget"):
                add_clock_region_policy(complete,route,architecture,root/"bad.json",maximum_clocks_per_region=0)

    def test_physical_bram_alias_uses_source_sealed_handoff_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);audit,route,mapped,_=self.fixture(root)
            arch_root=root/"arch";arch_root.mkdir();_,_,architecture=_fixture(arch_root,clock_regions=True,mixed=True)
            arch=json.loads(architecture.read_text());anchor="RAMB36E2_X0Y0"
            for site in arch["sites"]:
                if site["name"]==anchor:site["physical_region"]={"clock_region":"X0Y0","slr":"SLR0"}
            architecture.write_text(json.dumps(arch))
            assignment=dict(instance="br",cell_type="RAMB18E2",bel="RAMB18E2_L")
            packed=dict(schema="emuflow.packed-site-netlist/v1",clusters=[dict(id="br",kind="hard",site_templates=["RAMB36E2"],assignments=[assignment])],cascade_chains=[],unplaced_constants=[])
            placement=dict(schema="emuflow.xilinx-placement/v1",source={},clusters=[dict(cluster="br",site=anchor,site_type="RAMB36E2",x=12,y=0,fixed=False,assignments=[dict(assignment,site="RAMB18_ALIAS",placement_mode="RAMB18E2")])])
            handoff=root/"handoff.sqlite";write_xilinx_physical_handoff(handoff,packed,placement)
            metadata=dict(part="fixture",mapped_sha256=file_sha256(mapped),packed_sha256=file_sha256(handoff),placement_sha256=file_sha256(handoff))
            route.write_text("".join(f"META\t{k}\t{v}\n" for k,v in metadata.items())+"CELL\tc9\tbr\tRAMB18E2\tRAMB18_ALIAS\tRAMB18E2_L\nNET\tn20\tclock\nPIN\tn20\tc9\tCLKARDCLK\tsink\n")
            doc=json.loads(audit.read_text());doc.update(input_sha256=file_sha256(route),source=metadata);audit.write_text(json.dumps(doc))
            with self.assertRaisesRegex(ValidationError,"RAMB18_ALIAS"):
                add_clock_region_policy(audit,route,architecture,root/"missing.json",maximum_clocks_per_region=24)
            result=add_clock_region_policy(audit,route,architecture,root/"alias.json",maximum_clocks_per_region=24,physical_handoff_path=handoff)
            self.assertEqual(result["placement_domain"]["fixed_clock_region_loads"],1)
            self.assertEqual(result["clock_region_policy"]["physical_handoff_sha256"],file_sha256(handoff))
            doc["source"]["packed_sha256"]="0"*64;audit.write_text(json.dumps(doc))
            with self.assertRaisesRegex(ValidationError,"metadata differs"):
                add_clock_region_policy(audit,route,architecture,root/"bad.json",maximum_clocks_per_region=24,physical_handoff_path=handoff)

    def test_payload_tampering_and_source_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);audit,route,mapped,payload=self.fixture(root)
            with self.assertRaisesRegex(ValidationError,"separately"):
                complete_ideal_ff_clock_leaf_loads(audit,route,mapped,audit)
            payload.write_text(payload.read_text()+"\n")
            with self.assertRaisesRegex(ValidationError,"payload hash"):
                complete_ideal_ff_clock_leaf_loads(audit,route,mapped,root/"complete.json")


if __name__=="__main__":unittest.main()
