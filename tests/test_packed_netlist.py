import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.packed_netlist import (
    run_packed_netlist_import,
    validate_packed_netlist_contract,
    validate_packed_netlist_file,
)
from tests.native_build import vpr_packed_netlist_importer


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/physical/vpr_packed_fixture.net"


class PackedNetlistTest(unittest.TestCase):
    def test_resume_preserves_sealed_contract_bytes_after_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / "old", root / "new"
            old.mkdir()
            net = old / "partition.net"
            contract = old / "packed.json"
            shutil.copyfile(FIXTURE, net)
            executable = str(vpr_packed_netlist_importer())
            run_packed_netlist_import(net, contract, executable=executable)
            # Retained certificates may seal any valid JSON formatting.
            contract.write_text(json.dumps(read_json(contract), separators=(",", ":")))
            sealed = contract.read_bytes()
            shutil.copytree(old, new)
            before = (new / "packed.json").stat().st_mtime_ns
            report = run_packed_netlist_import(
                new / "partition.net", new / "packed.json",
                executable=executable, resume=True,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["output"], str((new / "packed.json").resolve()))
            self.assertEqual((new / "packed.json").read_bytes(), sealed)
            self.assertEqual((new / "packed.json").stat().st_mtime_ns, before)
            self.assertEqual(contract.read_bytes(), sealed)

    def test_resume_rejects_changed_contract_content_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "packed.json"
            executable = str(vpr_packed_netlist_importer())
            run_packed_netlist_import(FIXTURE, output, executable=executable)
            original = read_json(output)
            for field in ("clusters", "nets", "source"):
                with self.subTest(field=field):
                    changed = copy.deepcopy(original)
                    if field == "source":
                        changed[field]["sha256"] = "0" * 64
                    else:
                        changed[field] = []
                    output.write_text(json.dumps(changed))
                    sealed = output.read_bytes()
                    with self.assertRaisesRegex(ValidationError, "disagrees"):
                        run_packed_netlist_import(FIXTURE, output, executable=executable, resume=True)
                    self.assertEqual(output.read_bytes(), sealed)

    def test_resume_rejects_changed_native_input_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            net, output = root / "partition.net", root / "packed.json"
            executable = str(vpr_packed_netlist_importer())
            shutil.copyfile(FIXTURE, net)
            run_packed_netlist_import(net, output, executable=executable)
            sealed = output.read_bytes()
            net.write_bytes(net.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValidationError, "disagrees"):
                run_packed_netlist_import(net, output, executable=executable, resume=True)
            self.assertEqual(output.read_bytes(), sealed)
            alias = root / "alias.json"
            alias.symlink_to(output)
            with self.assertRaisesRegex(ValidationError, "regular file"):
                run_packed_netlist_import(net, alias, executable=executable, resume=True)

    def test_resume_missing_contract_runs_normal_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "packed.json"
            report = run_packed_netlist_import(
                FIXTURE, output, executable=str(vpr_packed_netlist_importer()), resume=True,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(validate_packed_netlist_file(output)["status"], "pass")

    def test_cpp_import_preserves_modes_hierarchy_and_cross_cluster_nets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "packed.json"
            report = run_packed_netlist_import(
                FIXTURE,
                output,
                executable=str(vpr_packed_netlist_importer()),
            )
            value = read_json(output)
            checked = validate_packed_netlist_file(output)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(checked["clusters"], 3)
        self.assertEqual(checked["block_types"], {"clb": 1, "io": 2})
        self.assertEqual(checked["cross_cluster_nets"], 2)
        self.assertEqual(checked["atoms"], 3)
        cluster = next(
            item for item in value["clusters"] if item["id"] == "clb[0]"
        )
        self.assertEqual(cluster["mode"], "default")
        self.assertEqual(cluster["atoms"], ["y"])
        self.assertEqual(
            [block["mode"] for block in cluster["pb_blocks"]],
            ["n1_lut6", "lut6", ""],
        )
        self.assertEqual(
            value["nets"],
            [
                {"id": "a", "driver": "io[1]", "sinks": ["clb[0]"]},
                {"id": "y", "driver": "clb[0]", "sinks": ["io[2]"]},
            ],
        )

    def test_validator_rejects_unknown_net_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "packed.json"
            run_packed_netlist_import(
                FIXTURE,
                output,
                executable=str(vpr_packed_netlist_importer()),
            )
            value = read_json(output)
        tampered = copy.deepcopy(value)
        tampered["nets"][0]["sinks"] = ["missing[0]"]
        with self.assertRaisesRegex(ValidationError, "sinks"):
            validate_packed_netlist_contract(tampered)

    def test_validator_checks_vpr_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "packed.json"
            run_packed_netlist_import(
                FIXTURE,
                output,
                executable=str(vpr_packed_netlist_importer()),
            )
            value = read_json(output)
        with self.assertRaisesRegex(ValidationError, "architecture_id"):
            validate_packed_netlist_contract(
                value,
                expected_architecture_sha256="2" * 64,
            )


if __name__ == "__main__":
    unittest.main()
