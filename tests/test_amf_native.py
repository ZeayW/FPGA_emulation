import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.amf_native import (
    AMF_NATIVE_CERTIFICATE_SCHEMA,
    probe_amf_native_executable,
    run_amf_native,
    validate_amf_native_certificate,
)
from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.xilinx_packing import pack_xilinx_sites
from emuflow.xilinx_placement import (
    XILINX_AMF_NATIVE_BRIDGE_PROVIDER,
    validate_xilinx_placement,
)


def _cell(cell_type, connections):
    directions = {
        port: (
            "output"
            if port in {"G", "P", "O", "O5", "O6", "Q", "CO"}
            else "input"
        )
        for port in connections
    }
    return {
        "type": cell_type,
        "parameters": {},
        "port_directions": directions,
        "connections": connections,
    }


def _mapped():
    cells = {
        "ground": _cell("GND", {"G": [900]}),
        "power": _cell("VCC", {"P": [901]}),
        "lut_main": _cell("LUT2", {"I0": [900], "I1": [901], "O": [500]}),
        "ff_main": _cell(
            "FDRE",
            {"C": ["0"], "CE": ["1"], "R": ["0"], "D": [500], "Q": [501]},
        ),
    }
    di = []
    select = []
    for index in range(8):
        di_bit = 1000 + index * 2
        select_bit = di_bit + 1
        cells[f"carry_lut_{index}"] = _cell(
            "LUT6_2",
            {
                "I0": [900], "I1": [901], "I2": ["0"],
                "I3": ["0"], "I4": ["0"], "I5": ["1"],
                "O5": [di_bit], "O6": [select_bit],
            },
        )
        di.append(di_bit)
        select.append(select_bit)
    cells["carry"] = _cell(
        "CARRY8",
        {
            "CI": ["0"], "CI_TOP": ["0"], "DI": di, "S": select,
            "CO": list(range(1100, 1108)), "O": list(range(1200, 1208)),
        },
    )
    return {"modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}}


def _architecture():
    lut_cells = [*[f"LUT{width}" for width in range(1, 7)], "LUT6_2"]
    ff_cells = ["FDCE", "FDPE", "FDRE", "FDSE"]
    bels = []
    for index, letter in enumerate("ABCDEFGH"):
        bels.append({
            "name": f"{letter}6LUT", "type": "LUT6", "z": index,
            "compatible_cells": lut_cells,
        })
        bels.append({
            "name": f"{letter}FF", "type": "FF", "z": 8 + index,
            "compatible_cells": ff_cells,
        })
    bels.append({
        "name": "CARRY8", "type": "CARRY8", "z": 16,
        "compatible_cells": ["CARRY8"],
    })
    return {
        "schema": "emuflow.archdb/v1",
        "part": "amf-native-fixture",
        "source": {"format": "unit-test/v1"},
        "policy": {"name": "amf-native-fixture"},
        "site_templates": {
            "SLICEL": {"bels": bels, "alternative_templates": []},
        },
        "sites": [
            {
                "name": f"SLICE_X0Y{y}", "type": "SLICEL",
                "template": "SLICEL", "x": 0, "y": y,
                "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            }
            for y in range(2)
        ],
    }


def _result_tcl(packed):
    lines = ["set result [catch {place_cell {"]
    for cluster in sorted(packed["clusters"], key=lambda item: item["id"]):
        site = "SLICE_X0Y0" if cluster["kind"] == "carry" else "SLICE_X0Y1"
        for assignment in cluster["assignments"]:
            lines.append(f"  {assignment['instance']} {site}/{assignment['bel']}")
    lines.append("}}]")
    return "\n".join(lines) + "\n"


def _test_double(path, tcl, *, complete=True):
    markers = [
        "InitialPacker Finding unpacked units",
        "GlobalPlacer GlobalPlacement_CLBElements started",
        "ParallelCLBPacker: dumping placementTcl archieve",
    ]
    if complete:
        markers.append("Placement Done")
    source = f'''#!/usr/bin/env python3
import json
import pathlib
import sys
if len(sys.argv) < 2:
    print("Usage: AMFPlacer <config JSON file> [-gui]")
    raise SystemExit(1)
config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for marker in {markers!r}:
    print(marker)
pathlib.Path(config["DumpCLBPacking"] + "-first-0.tcl").write_text({tcl!r}, encoding="utf-8")
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


class AMFNativeRunnerTest(unittest.TestCase):
    def _inputs(self, root):
        mapped = _mapped()
        mapped_path = root / "mapped.json"
        packed_path = root / "packed.json"
        architecture_path = root / "architecture.json"
        write_json(mapped_path, mapped, compact=True)
        packed = pack_xilinx_sites(mapped_path, packed_path, top="top")
        write_json(architecture_path, _architecture(), compact=True)
        return mapped, mapped_path, packed, packed_path, architecture_path

    def test_subprocess_protocol_emits_test_only_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _mapped_value, mapped_path, packed, packed_path, architecture_path = self._inputs(root)
            executable = root / "AMFPlacer-test-double"
            _test_double(executable, _result_tcl(packed))
            manifest = probe_amf_native_executable(
                executable, source_revision="test-double", runtime_kind="test-double"
            )
            manifest_path = root / "manifest.json"
            placement_path = root / "placement.json"
            certificate_path = root / "certificate.json"
            write_json(manifest_path, manifest, compact=True)

            report = run_amf_native(
                mapped_path=mapped_path,
                packed_path=packed_path,
                architecture_path=architecture_path,
                executable=executable,
                manifest_path=manifest_path,
                placement_path=placement_path,
                certificate_path=certificate_path,
                work_root=root / "work",
                top="top",
                allow_test_double=True,
            )
            self.assertEqual(report["validation"]["status"], "test-only")
            self.assertEqual(
                report["certificate"]["schema"], AMF_NATIVE_CERTIFICATE_SCHEMA
            )
            placement = json.loads(placement_path.read_text(encoding="utf-8"))
            self.assertEqual(placement["provider"], XILINX_AMF_NATIVE_BRIDGE_PROVIDER)
            self.assertEqual(
                validate_xilinx_placement(
                    packed_path, architecture_path, placement_path
                )["status"],
                "pass",
            )
            self.assertEqual(list((root / "work").iterdir()), [])

            with self.assertRaisesRegex(ValidationError, "not native evidence"):
                validate_amf_native_certificate(
                    report["certificate"],
                    executable=executable,
                    manifest=manifest,
                    packed_path=packed_path,
                    architecture_path=architecture_path,
                    placement_path=placement_path,
                )

            tampered = copy.deepcopy(report["certificate"])
            tampered["assignment_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValidationError, "assignment seal"):
                validate_amf_native_certificate(
                    tampered,
                    executable=executable,
                    manifest=manifest,
                    packed_path=packed_path,
                    architecture_path=architecture_path,
                    placement_path=placement_path,
                    allow_test_double=True,
                )

    def test_test_double_and_incomplete_core_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _mapped_value, mapped_path, packed, packed_path, architecture_path = self._inputs(root)
            executable = root / "AMFPlacer-test-double"
            _test_double(executable, _result_tcl(packed), complete=False)
            manifest = probe_amf_native_executable(
                executable, source_revision="test-double", runtime_kind="test-double"
            )
            manifest_path = root / "manifest.json"
            write_json(manifest_path, manifest, compact=True)
            arguments = {
                "mapped_path": mapped_path,
                "packed_path": packed_path,
                "architecture_path": architecture_path,
                "executable": executable,
                "manifest_path": manifest_path,
                "placement_path": root / "placement.json",
                "certificate_path": root / "certificate.json",
                "work_root": root / "work",
                "top": "top",
            }
            with self.assertRaisesRegex(ValidationError, "cannot be used as native"):
                run_amf_native(**arguments)
            with self.assertRaisesRegex(ValidationError, "did not prove stages"):
                run_amf_native(**arguments, allow_test_double=True)

            cascaded = json.loads(packed_path.read_text(encoding="utf-8"))
            cascaded["cascade_chains"] = [{"type": "CARRY8", "instances": ["carry"]}]
            write_json(packed_path, cascaded, compact=True)
            with self.assertRaisesRegex(ValidationError, "cascade qualification"):
                run_amf_native(**arguments, allow_test_double=True)

    def test_real_clock_and_native_manifest_requirements_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, mapped_path, packed, packed_path, architecture_path = self._inputs(root)
            mapped["modules"]["top"]["cells"]["ff_main"]["connections"]["C"] = [700]
            write_json(mapped_path, mapped, compact=True)
            executable = root / "AMFPlacer-test-double"
            _test_double(executable, _result_tcl(packed))
            manifest = probe_amf_native_executable(
                executable, source_revision="test-double", runtime_kind="test-double"
            )
            manifest_path = root / "manifest.json"
            write_json(manifest_path, manifest, compact=True)
            with self.assertRaisesRegex(ValidationError, "clock legality"):
                run_amf_native(
                    mapped_path=mapped_path,
                    packed_path=packed_path,
                    architecture_path=architecture_path,
                    executable=executable,
                    manifest_path=manifest_path,
                    placement_path=root / "placement.json",
                    certificate_path=root / "certificate.json",
                    work_root=root / "work",
                    top="top",
                    allow_test_double=True,
                )
            with self.assertRaisesRegex(ValidationError, "revision is not pinned"):
                probe_amf_native_executable(
                    executable,
                    source_revision="wrong",
                    runtime_kind="upstream-native",
                    portability_patch=root / "missing.patch",
                )
            unreviewed_patch = root / "unreviewed.patch"
            unreviewed_patch.write_text("not the reviewed patch\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "not the reviewed patch"):
                probe_amf_native_executable(
                    executable,
                    source_revision="70d98288153046ea4fd07190e748b6530e3042f5",
                    runtime_kind="upstream-native",
                    portability_patch=unreviewed_patch,
                )


if __name__ == "__main__":
    unittest.main()
