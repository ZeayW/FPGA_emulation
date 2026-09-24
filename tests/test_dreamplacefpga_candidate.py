import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.dreamplacefpga_candidate import (
    DREAMPLACEFPGA_CAPABILITY_SCHEMA,
    DREAMPLACEFPGA_UPSTREAM_REVISION,
    assess_dreamplacefpga_candidate,
    require_dreamplacefpga_execution_ready,
    run_dreamplacefpga_interchange_probe,
)
from emuflow.errors import ValidationError
from emuflow.xilinx_placer_capability import (
    XILINX_PLACER_REQUIRED_STAGES,
    validate_xilinx_placer_capability_report,
)


def _cell(cell_type):
    return {
        "type": cell_type,
        "port_directions": {"I": "input", "O": "output"},
        "connections": {"I": [0], "O": [1]},
    }


def _mapped(path, *cell_types):
    path.write_text(json.dumps({
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "cells": {
                    f"u{index}": _cell(cell_type)
                    for index, cell_type in enumerate(cell_types)
                },
            }
        }
    }), encoding="utf-8")


class DreamplaceFPGACandidateTest(unittest.TestCase):
    def test_report_uses_common_capability_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            _mapped(mapped, "LUT6", "FDRE")
            report = assess_dreamplacefpga_candidate(mapped)
        checked = validate_xilinx_placer_capability_report(report)
        self.assertEqual(checked["schema"], DREAMPLACEFPGA_CAPABILITY_SCHEMA)
        self.assertEqual(checked["revision"], DREAMPLACEFPGA_UPSTREAM_REVISION)
        self.assertEqual(set(checked["stages"]), set(XILINX_PLACER_REQUIRED_STAGES))
        self.assertEqual(
            report["observed_primitives"]["LUT6"]["status"],
            "native_supported",
        )
        self.assertFalse(report["execution_ready"])
        self.assertIn("stages.detailed_placement", report["blockers"])
        self.assertIn("constraints.multi_slr_regions", report["blockers"])
        self.assertEqual(
            report["feature_capabilities"]["emuflow_mapped_json_input"][
                "adapter_validation"
            ],
            "pass",
        )
        self.assertEqual(
            report["feature_capabilities"]["emuflow_placement_import"][
                "adapter_validation"
            ],
            "pass",
        )

    def test_full_emuflow_primitive_population_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            _mapped(
                mapped,
                "CARRY8",
                "MUXF8",
                "RAMB18E2",
                "URAM288",
                "FDCE",
            )
            report = assess_dreamplacefpga_candidate(mapped)
        for cell_type in ("CARRY8", "MUXF8", "RAMB18E2", "URAM288", "FDCE"):
            self.assertEqual(
                report["observed_primitives"][cell_type]["status"],
                "core_missing",
            )
            self.assertIn(f"primitives.{cell_type}", report["blockers"])
        with self.assertRaisesRegex(ValidationError, "not execution-ready"):
            require_dreamplacefpga_execution_ready(report)

    def test_runtime_probe_does_not_accept_source_tree_without_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "dreamplacefpga"
            package.mkdir()
            (package / "Placer.py").write_text("", encoding="utf-8")
            (package / "IFWriter.py").write_text("", encoding="utf-8")
            (package / "configure.py").write_text("", encoding="utf-8")
            mapped = root / "mapped.json"
            _mapped(mapped, "LUT6")
            report = assess_dreamplacefpga_candidate(
                mapped, dreamplace_root=root
            )
        self.assertEqual(report["runtime"]["state"], "core_missing")
        self.assertIn("compiled_place_io", report["runtime"]["missing"])

    def test_probe_runner_is_bounded_and_marks_output_diagnostic(self):
        def entry(status="native_supported"):
            return {"status": status, "evidence": ["fixture"]}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            placer = root / "dreamplacefpga" / "Placer.py"
            placer.parent.mkdir()
            placer.write_text("", encoding="utf-8")
            device = root / "fixture.device"
            netlist = root / "fixture.netlist"
            device.write_bytes(b"device")
            netlist.write_bytes(b"netlist")
            output = root / "run"
            report = {
                "schema": DREAMPLACEFPGA_CAPABILITY_SCHEMA,
                "provider": "fixture",
                "revision": "fixture-revision",
                "stages": {
                    stage: entry() for stage in XILINX_PLACER_REQUIRED_STAGES
                },
                "primitives": {"LUT6": entry()},
                "constraints": {"site_bel_compatibility": entry()},
                "runtime": {"root": str(root)},
                "inputs": {
                    "device": {"path": str(device)},
                    "logical_netlist": {"path": str(netlist)},
                },
                "execution_ready": True,
                "blockers": [],
            }

            def fake_run(*args, **kwargs):
                target = output / "results" / "fixture"
                target.mkdir(parents=True)
                with gzip.open(target / "placed.phys", "wb") as stream:
                    stream.write(b"physical-netlist")
                return subprocess.CompletedProcess(args[0], 0, "", "")

            with mock.patch(
                "emuflow.dreamplacefpga_candidate.subprocess.run",
                side_effect=fake_run,
            ) as invoked:
                result = run_dreamplacefpga_interchange_probe(report, output)
        self.assertEqual(result["status"], "pass")
        self.assertIn("diagnostic", result["qualification_boundary"])
        command = invoked.call_args.args[0]
        self.assertEqual(command[1], str(placer))

    def test_malformed_phys_output_is_rejected(self):
        def entry():
            return {"status": "native_supported", "evidence": ["fixture"]}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            placer = root / "dreamplacefpga" / "Placer.py"
            placer.parent.mkdir()
            placer.write_text("", encoding="utf-8")
            device = root / "fixture.device"
            netlist = root / "fixture.netlist"
            device.write_bytes(b"device")
            netlist.write_bytes(b"netlist")
            output = root / "run"
            report = {
                "schema": DREAMPLACEFPGA_CAPABILITY_SCHEMA,
                "provider": "fixture",
                "revision": "fixture-revision",
                "stages": {
                    stage: entry() for stage in XILINX_PLACER_REQUIRED_STAGES
                },
                "primitives": {"LUT6": entry()},
                "constraints": {"site_bel_compatibility": entry()},
                "runtime": {"root": str(root)},
                "inputs": {
                    "device": {"path": str(device)},
                    "logical_netlist": {"path": str(netlist)},
                },
                "execution_ready": True,
                "blockers": [],
            }

            def fake_run(*args, **kwargs):
                target = output / "results" / "fixture"
                target.mkdir(parents=True)
                (target / "placed.phys").write_bytes(b"not-gzip")
                return subprocess.CompletedProcess(args[0], 0, "", "")

            with mock.patch(
                "emuflow.dreamplacefpga_candidate.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaisesRegex(ValidationError, "valid gzip"):
                    run_dreamplacefpga_interchange_probe(report, output)


if __name__ == "__main__":
    unittest.main()
