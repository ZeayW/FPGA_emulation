import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.xilinx_lut_depopulation import repair_clock_leaf_domains
from emuflow.xilinx_openparf_atomic import load_xilinx_openparf_atomic_sites
from tests import test_xilinx_lut_depopulation as depopulation_tests


class NativeClockLeafRepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("c++")
        if compiler is None:
            raise unittest.SkipTest("C++17 compiler unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.build.name)/"repair"
        subprocess.run([compiler, "-std=c++17", "-O2", str(Path(__file__).parents[1]/"src/native/xilinx_clock_leaf_repair.cpp"), "-o", str(cls.exe)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_case(self, root, clocks, *, targets=2, fixed=(), macro=(), capacity=2, target_capacity=2, corrupt=None):
        sites, atoms, placement, domains, output = [root/n for n in ("sites", "atoms", "placement", "domains", "output")]
        site_rows=[]; atom_rows=[]; pl_rows=[]; domain_rows=[f"D 0 {capacity}\n",f"D 1 {target_capacity}\n"]
        for i, clock in enumerate(clocks):
            site_rows.append(f"{100+i} 10 {i} 0 1\n")
            domain_rows.extend([f"S SLICE_X{i}Y0 0 0\n",f"C SLICE_X{i}Y0 CLK1 {clock}\n"])
            for j, (resource, kind, z) in enumerate((("LUT","LUT6",1),("FF","FDRE",0))):
                name=f"a{i}_{j}"; cluster=f"macro{i}" if i in macro else name
                atom_rows.append(f"{name} {cluster} {resource} {kind}\n")
                pl_rows.append(f"{name} {100+i} 10 {z}"+(" /FIXED" if i in fixed and j==0 else "")+"\n")
        for i in range(len(clocks),len(clocks)+targets):
            site_rows.append(f"{100+i} 10 {i} 0 0\n");domain_rows.append(f"S SLICE_X{i}Y0 1 1\n")
        if corrupt=="duplicate":domain_rows.append(domain_rows[3])
        elif corrupt=="missing_clock":domain_rows=[v for v in domain_rows if not v.startswith("C SLICE_X0Y0")]
        elif corrupt=="domain":domain_rows[2]="S SLICE_X0Y0 99 0\n"
        elif corrupt=="extra":domain_rows.append("S SLICE_X9Y0 0 0 extra\n")
        sites.write_text("".join(site_rows));atoms.write_text("".join(atom_rows));placement.write_text("".join(pl_rows));domains.write_text("".join(domain_rows))
        before=placement.read_bytes()
        result=subprocess.run([str(self.exe),str(sites),str(atoms),str(placement),str(output),str(domains),"20"],text=True,capture_output=True)
        self.assertEqual(placement.read_bytes(),before)
        return result,output

    def test_repairs_overfull_domain_and_keeps_rigid_slice_bels(self):
        with tempfile.TemporaryDirectory() as d:
            result,output=self.run_case(Path(d),["a","b","c"])
            self.assertEqual(result.returncode,0,result.stderr)
            counters=json.loads(result.stdout)
            self.assertEqual((counters["overfull_domains_before"],counters["overfull_domains_after"]),(1,0))
            self.assertEqual((counters["moved_slices"],counters["moved_luts"],counters["moved_ffs"]),(1,1,1))
            rows={v[0]:v[1:] for v in map(str.split,output.read_text().splitlines())}
            self.assertEqual(rows["a0_0"][:2],rows["a0_1"][:2])
            self.assertEqual((rows["a0_0"][2],rows["a0_1"][2]),("1","0"))

    def test_fixed_and_macro_groups_never_move(self):
        with tempfile.TemporaryDirectory() as d:
            result,output=self.run_case(Path(d),["a","b","c"],fixed=(0,),macro=(1,))
            self.assertEqual(result.returncode,0,result.stderr)
            rows={v[0]:v[1:] for v in map(str.split,output.read_text().splitlines())}
            self.assertEqual(rows["a0_0"][0],"100");self.assertEqual(rows["a1_0"][0],"101")
            self.assertNotEqual(rows["a2_0"][0],"102")
            self.assertEqual(json.loads(result.stdout)["overfull_domains_after"],0)

    def test_failed_multi_slice_transactions_leave_all_original_groups_intact(self):
        with tempfile.TemporaryDirectory() as d:
            result,output=self.run_case(Path(d),["a","a","b","b","c","c"],targets=1)
            self.assertEqual(result.returncode,0,result.stderr)
            counters=json.loads(result.stdout)
            self.assertEqual(counters["moved_slices"],0)
            self.assertEqual(counters["overfull_domains_after"],1)
            self.assertEqual(counters["failed_transactions"],3)
            rows={v[0]:v[1:] for v in map(str.split,output.read_text().splitlines())}
            self.assertTrue(all(rows[f"a{i}_0"][0]==str(100+i) for i in range(6)))

    def test_failed_coupled_clock_move_rolls_back_then_another_clock_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/"sites").write_text("100 10 0 0 1\n101 10 1 0 1\n102 10 2 0 0\n")
            (root/"atoms").write_text("a a FF FDRE\nx x FF FDRE\nb b FF FDRE\nl l LUT LUT6\n")
            (root/"placement").write_text("a 100 10 0\nx 100 10 8\nb 101 10 0\nl 101 10 1\n")
            (root/"domains").write_text("D 0 2\nD 1 1\nS SLICE_X0Y0 0 0\nS SLICE_X1Y0 0 0\nS SLICE_X2Y0 1 1\nC SLICE_X0Y0 CLK1 a\nC SLICE_X0Y0 CLK2 x\nC SLICE_X1Y0 CLK1 b\n")
            result=subprocess.run([str(self.exe),*[str(root/n) for n in ["sites","atoms","placement","output","domains"]],"20"],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            counters=json.loads(result.stdout)
            self.assertEqual(counters["failed_transactions"],1)
            self.assertEqual(counters["overfull_domains_after"],0)
            rows={v[0]:v[1:] for v in map(str.split,(root/"output").read_text().splitlines())}
            self.assertEqual(rows["a"][:2],["100","10"])
            self.assertEqual(rows["x"][:2],["100","10"])
            self.assertEqual(rows["b"][:2],["102","10"])
            self.assertEqual(rows["l"][:2],["102","10"])

    def test_shared_clock_counts_once_per_domain(self):
        with tempfile.TemporaryDirectory() as d:
            result,_=self.run_case(Path(d),["a","a","b"])
            self.assertEqual(result.returncode,0,result.stderr)
            counters=json.loads(result.stdout)
            self.assertEqual(counters["excess_clocks_before"],0)
            self.assertEqual(counters["moved_slices"],0)

    def test_regional_budget_is_enforced_alongside_leaf_capacity(self):
        for blocked_target in (False,True):
            with self.subTest(blocked_target=blocked_target),tempfile.TemporaryDirectory() as d:
                root=Path(d);self.run_case(root,["a","b","c"],targets=2)
                text=(root/"domains").read_text().replace("D 0 2", "D 0 16")
                text+="D 2 2 region\nD 3 2 region\n"
                for i in range(5):text+=f"R SLICE_X{i}Y0 {2 if i<3 else 3}\n"
                if blocked_target:text+="P 3 fixed0\nP 3 fixed1\n"
                (root/"domains").write_text(text)
                output=root/"regional-output"
                result=subprocess.run([str(self.exe),*[str(root/n) for n in ["sites","atoms","placement"]],str(output),str(root/"domains"),"20"],text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
                counters=json.loads(result.stdout)
                self.assertEqual(counters["overfull_clock_regions_before"],1)
                self.assertEqual(counters["overfull_clock_regions_after"],int(blocked_target))
                self.assertEqual(counters["moved_slices"],0 if blocked_target else 1)

    def test_partial_regional_site_coverage_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.run_case(root,["a","b","c"])
            (root/"domains").write_text((root/"domains").read_text()+"D 2 2 region\nR SLICE_X0Y0 2\n")
            result=subprocess.run([str(self.exe),*[str(root/n) for n in ["sites","atoms","placement"]],str(root/"bad-output"),str(root/"domains"),"20"],text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/"bad-output").exists())

    def test_stalled_region_is_retried_after_another_region_frees_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            clocks=["b","a","c","a","d","e"]
            (root/"sites").write_text("".join(f"{100+i} 10 {i} 0 1\n" for i in range(7)))
            (root/"atoms").write_text("".join(f"a{i} a{i} FF FDRE\n" for i in range(6)))
            (root/"placement").write_text("".join(f"a{i} {100+i} 10 0"+(" /FIXED" if i==3 else "")+"\n" for i in range(6)))
            domains="D 0 16\nD 10 2 region\nD 11 2 region\nD 12 2 region\n"
            for i in range(7):
                domains+=f"S SLICE_X{i}Y0 0 0\nR SLICE_X{i}Y0 {10 if i<3 else 11 if i<6 else 12}\n"
                if i<6:domains+=f"C SLICE_X{i}Y0 CLK1 {clocks[i]}\n"
            (root/"domains").write_text(domains)
            result=subprocess.run([str(self.exe),*[str(root/n) for n in ["sites","atoms","placement","output","domains"]],"3"],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            counters=json.loads(result.stdout)
            self.assertEqual(counters["overfull_clock_regions_before"],2)
            self.assertEqual(counters["overfull_domains_after"],0)
            self.assertEqual(counters["moved_slices"],2)
            self.assertGreater(counters["failed_transactions"],0)
            rows={v[0]:v[1:] for v in map(str.split,(root/"output").read_text().splitlines())}
            self.assertEqual(rows["a1"][:2],["104","10"])
            self.assertEqual(rows["a3"],["103","10","0","/FIXED"])
            self.assertEqual(rows["a4"][:2],["106","10"])

    def test_wrapper_binds_domain_payload_route_input_and_source_certificate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            placement,name_map,kwargs=depopulation_tests.NativeLutDepopulationTest.wrapper_fixture(self,root)
            sites=load_xilinx_openparf_atomic_sites(name_map,resources=["LUT"],mapped_sha256=kwargs["mapped_sha256"],packed_sha256=kwargs["packed_sha256"])
            domain=root/"domains.tsv"
            domain.write_text("D 0 16\n"+"".join(f"S {v['physical_sites']['LUT'][0]} 0 0\n" for v in sites)+f"C {sites[0]['physical_sites']['LUT'][0]} CLK1 clock\n")
            metadata=dict(part="fixture",mapped_sha256=kwargs["mapped_sha256"],placement_sha256="a"*64)
            route_input=root/"rwroute.tsv";route_input.write_text("".join(f"META\t{k}\t{v}\n" for k,v in metadata.items()))
            certificate=root/"certificate.json"
            certificate.write_text(json.dumps(dict(status="pass",part="fixture",source=dict(native_placement_sha256=file_sha256(placement),mapped_sha256=kwargs["mapped_sha256"],packed_sha256=kwargs["packed_sha256"]))))
            audit=root/"audit.json";value=dict(schema="emuflow.clock-leaf-domain-audit/v1",input_sha256=file_sha256(route_input),source=metadata,placement_domain=dict(schema="emuflow.clock-leaf-placement-domain/v1",path=domain.name,sha256=file_sha256(domain)))
            audit.write_text(json.dumps(value))
            args=dict(**kwargs,hot_clock_regions=["X0Y0"],clock_domain_audit=audit,route_input_path=route_input,placement_certificate_path=certificate)
            with patch("emuflow.xilinx_lut_depopulation.resolve_native_executable",return_value=str(self.exe)):
                report=repair_clock_leaf_domains(placement,name_map,root/"candidate.pl",**args)
                self.assertEqual(report["counters"]["atoms"],2)
                self.assertEqual(report["counters"]["overfull_domains_after"],0)
                domain.write_text(domain.read_text()+"\n")
                with self.assertRaisesRegex(ValidationError,"payload hash differs"):
                    repair_clock_leaf_domains(placement,name_map,root/"bad.pl",**args)
                self.assertFalse((root/"bad.pl").exists())

    def test_invalid_domain_or_clock_coverage_fails_before_publication(self):
        for corrupt in ("duplicate","missing_clock","domain","extra"):
            with self.subTest(corrupt=corrupt),tempfile.TemporaryDirectory() as d:
                result,output=self.run_case(Path(d),["a","b","c"],corrupt=corrupt)
                self.assertNotEqual(result.returncode,0,result.stderr)
                self.assertFalse(output.exists())


if __name__=="__main__":unittest.main()
