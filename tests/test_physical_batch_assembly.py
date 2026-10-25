import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, write_json
from emuflow.netlist import SPLIT_MANIFEST_SCHEMA
from emuflow.physical_backend import physical_backend_descriptor
from emuflow.physical_batch_assembly import assemble_vivado_physical_batches
from emuflow.platform import Platform
from emuflow.runtime import build_virtual_runtime
from emuflow.tdm_schedule_storage import canonical_tdm_schedule_sha256, read_tdm_schedule
from emuflow.tdm import TDM_SCHEDULE_SCHEMA
from emuflow.vivado_clocks import implementation_timing_contract


PLATFORM = Path(__file__).resolve().parents[1] / "platforms/virtual/academic_vtr_2fpga_p2p.json"


class PhysicalBatchAssemblyTest(unittest.TestCase):
    def prepare(self, root):
        split = root / "split"
        split.mkdir()
        write_json(split / "manifest.json", {
            "schema": SPLIT_MANIFEST_SCHEMA, "design": "design",
            "clock_periods": {"clk": 10.0},
        })
        platform = Platform.load(PLATFORM)
        schedule = root / "schedule.json"
        write_json(schedule, {"schema": TDM_SCHEDULE_SCHEMA, "platform": platform.name,
                              "metrics": {"frame_slots": 1024, "completion_slot": 0},
                              "entries": []})
        runtime = build_virtual_runtime(read_tdm_schedule(schedule), platform, {"clk": 10.0})
        source = {name: {"sha256": file_sha256(path)} for name, path in (
            ("split_manifest", split / "manifest.json"), ("platform", PLATFORM), ("schedule", schedule))}
        batches = []
        for fpga in platform.fpgas:
            worker = root / fpga.id
            worker.mkdir()
            digest = hashlib.sha256(json.dumps(
                {**runtime, "resource_budget": fpga.to_dict()}, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            native = {name: {"status": "pass"} for name in (
                "boundary_timing", "logic_segment_timing", "local_path_timing")}
            native["input_identity"] = {"runtime_sha256": digest}
            native["implementation_options"] = {"place_directive": "Default", "route_directive": "Default", "dut_frequency_mhz": None}
            native["implementation_timing"] = implementation_timing_contract(runtime)
            record = worker / "partition-record.json"
            write_json(record, {"fpga": fpga.id, "status": "pass", "active": True,
                                "stages": {"vivado_implementation": native}})
            path = worker / "physical-partition-batch.json"
            batch = {"schema": "emuflow.physical-partition-batch/v1", "status": "pass",
                     "backend": physical_backend_descriptor("vivado"),
                     "architecture": {"status": "pass", "mode": "platform-parts",
                                      "parts": sorted({item.part for item in platform.fpgas}),
                                      "provider": "vivado-device-database"},
                     "expected_fpgas": [item.id for item in platform.fpgas],
                     "selected_fpgas": [fpga.id], "source": copy.deepcopy(source),
                     "execution": {"seed": 1, "vivado_implementation": {
                         "place_directive": "Default", "route_directive": "Default", "dut_frequency_mhz": None}},
                     "records": [{"fpga": fpga.id, "path": record.name,
                                  "sha256": file_sha256(record)}]}
            write_json(path, batch)
            batches.append(path)
        return split, schedule, batches

    def test_join_passes_checked_records_to_common_validator_without_tool_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split, schedule, batches = self.prepare(root)
            with patch("emuflow.physical_batch_assembly._assemble_multi_fpga_physical_results") as join:
                join.return_value = {"summary": {"status": "pass"}}
                result = assemble_vivado_physical_batches(split, PLATFORM, schedule, batches[::-1], root / "joined")
                self.assertEqual(result["summary"]["status"], "pass")
                inputs = join.call_args.kwargs
                self.assertEqual({r["fpga"] for r in inputs["records"]}, {"fpga0", "fpga1"})
                self.assertTrue(inputs["include_logic_timing"])
                self.assertEqual(inputs["execution"]["worker_batches"], 2)
                self.assertFalse((root / "joined").exists())

    def test_fixed_io_binding_is_recomputed_instead_of_trusting_passing_status(self):
        for change in ("unchanged", "native-contract", "binding-schedule"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                split, schedule, batches = self.prepare(root)
                manifest_path = split / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["virtual_board_binding"] = {
                    "wiring": "board.json", "binding": "binding.json",
                    "xdc": {fpga: f"{fpga}.xdc" for fpga in ("fpga0", "fpga1")},
                    "differential_io": {fpga: f"{fpga}.sv" for fpga in ("fpga0", "fpga1")}}
                write_json(manifest_path, manifest)
                write_json(split / "board.json", {})
                binding = {"board_sha256": hashlib.sha256(b"{}").hexdigest(),
                           "schedule_sha256": canonical_tdm_schedule_sha256(read_tdm_schedule(schedule))}
                if change == "binding-schedule":
                    binding["schedule_sha256"] = "0" * 64
                contract = {"pins": ["P", "N"]}
                contract_sha = hashlib.sha256(b'{"pins":["P","N"]}').hexdigest()
                for path in batches:
                    batch = json.loads(path.read_text())
                    batch["source"]["split_manifest"]["sha256"] = file_sha256(manifest_path)
                    record_path = path.parent / "partition-record.json"
                    record = json.loads(record_path.read_text())
                    record["stages"]["virtual_io_implementation"] = {
                        "status": "pass", "contract_sha256": "0" * 64 if change == "native-contract" else contract_sha}
                    write_json(record_path, record)
                    batch["records"][0]["sha256"] = file_sha256(record_path)
                    write_json(path, batch)
                with patch("emuflow.physical_batch_assembly.validate_virtual_board"), patch(
                    "emuflow.physical_batch_assembly.read_virtual_binding", return_value=binding
                ), patch("emuflow.physical_batch_assembly.build_virtual_io_physical_contract", return_value=contract), patch(
                    "emuflow.physical_batch_assembly._assemble_multi_fpga_physical_results"
                ) as join:
                    if change == "unchanged":
                        assemble_vivado_physical_batches(split, PLATFORM, schedule, batches, root / "joined")
                        join.assert_called_once()
                    else:
                        with self.assertRaisesRegex(ValidationError, "fixed I/O"):
                            assemble_vivado_physical_batches(split, PLATFORM, schedule, batches, root / "joined")
                        join.assert_not_called()

    def test_mixed_sources_options_digest_and_incomplete_coverage_fail_before_publication(self):
        for mutation in ("source", "seed", "directive", "missing-options", "digest", "missing-fpga", "duplicate-fpga", "runtime", "missing-timing", "reported-directive", "runtime-clock", "missing-clock-policy", "mixed-frequency", "missing-frequency"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                split, schedule, batches = self.prepare(root)
                path = batches[1]
                batch = json.loads(path.read_text())
                if mutation == "source":
                    batch["source"]["schedule"]["sha256"] = "0" * 64
                elif mutation == "seed":
                    batch["execution"]["seed"] = 2
                elif mutation == "directive":
                    batch["execution"]["vivado_implementation"]["route_directive"] = "Quick"
                elif mutation == "mixed-frequency":
                    batch["execution"]["vivado_implementation"]["dut_frequency_mhz"] = 10.0
                elif mutation == "missing-frequency":
                    del batch["execution"]["vivado_implementation"]["dut_frequency_mhz"]
                elif mutation == "missing-options":
                    del batch["execution"]["vivado_implementation"]
                elif mutation == "digest":
                    batch["records"][0]["sha256"] = "0" * 64
                elif mutation == "missing-fpga":
                    batches = batches[:1]
                elif mutation == "duplicate-fpga":
                    batches = [batches[0], batches[0]]
                else:
                    record_path = path.parent / "partition-record.json"
                    record = json.loads(record_path.read_text())
                    native = record["stages"]["vivado_implementation"]
                    if mutation == "runtime":
                        native["input_identity"]["runtime_sha256"] = "0" * 64
                    elif mutation == "reported-directive":
                        native["implementation_options"]["place_directive"] = "Quick"
                    elif mutation == "runtime-clock":
                        native["implementation_timing"]["dut_clock_periods_ns"]["clk"] = 4096.0
                    elif mutation == "missing-clock-policy":
                        del native["implementation_timing"]
                    else:
                        del native["local_path_timing"]
                    write_json(record_path, record)
                    batch["records"][0]["sha256"] = file_sha256(record_path)
                write_json(path, batch)
                with patch("emuflow.physical_batch_assembly._assemble_multi_fpga_physical_results") as join:
                    with self.assertRaises(ValidationError):
                        assemble_vivado_physical_batches(split, PLATFORM, schedule, batches, root / "joined")
                    join.assert_not_called()
                self.assertFalse((root / "joined").exists())
