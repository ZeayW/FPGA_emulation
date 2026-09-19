import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    audit_xilinx_mapped_json,
    load_xilinx_primitive_library,
    normalize_xilinx_mapped_json,
)


def _cell(cell_type: str) -> dict:
    return {
        "type": cell_type,
        "port_directions": {"I": "input", "O": "output"},
        "connections": {"I": [0], "O": [1]},
    }


class XilinxPrimitiveContractTest(unittest.TestCase):
    def test_checked_in_library_is_self_consistent(self) -> None:
        _, report = load_xilinx_primitive_library()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["mapping_profile"], XILINX_ULTRASCALEPLUS_OPEN_PROFILE
        )
        for required in (
            "LUT6",
            "FDRE",
            "CARRY8",
            "DSP48E2",
            "RAMB18E2",
            "RAMB36E2",
            "URAM288",
        ):
            self.assertIn(required, report["cells"])

    def test_mapped_design_is_audited_fail_closed(self) -> None:
        cells = {
            f"u{index}": _cell(cell_type)
            for index, cell_type in enumerate(
                (
                    "LUT6",
                    "FDRE",
                    "CARRY8",
                    "DSP48E2",
                    "RAMB18E2",
                    "RAMB36E2",
                    "URAM288",
                )
            )
        }
        value = {
            "modules": {
                "top": {"attributes": {"top": "1"}, "cells": cells}
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mapped.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            report = audit_xilinx_mapped_json(path, top="top")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["instances"], 7)
        self.assertEqual(report["resource_totals"]["lut"], 1)
        self.assertEqual(report["resource_totals"]["ff"], 1)
        self.assertEqual(report["resource_totals"]["carry8"], 1)
        self.assertEqual(report["resource_totals"]["dsp48"], 1)
        self.assertEqual(report["resource_totals"]["bram18k"], 3)
        self.assertEqual(report["resource_totals"]["uram288"], 1)

    def test_unknown_cell_is_rejected(self) -> None:
        value = {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "cells": {"bad": _cell("$add")},
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mapped.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "unsupported Xilinx primitive"
            ):
                audit_xilinx_mapped_json(path, top="top")

    def test_yosys_carry4_pairs_and_inv_are_normalized(self) -> None:
        def carry(ci, cyinit, base):
            return {
                "type": "CARRY4",
                "port_directions": {
                    "CI": "input",
                    "CYINIT": "input",
                    "DI": "input",
                    "S": "input",
                    "CO": "output",
                    "O": "output",
                },
                "connections": {
                    "CI": [ci],
                    "CYINIT": [cyinit],
                    "DI": list(range(base, base + 4)),
                    "S": list(range(base + 4, base + 8)),
                    "CO": list(range(base + 8, base + 12)),
                    "O": list(range(base + 12, base + 16)),
                },
            }

        first = carry("0", 2, 10)
        second = carry(first["connections"]["CO"][3], "0", 40)
        value = {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "carry0": first,
                        "carry1": second,
                        "inv": {
                            "type": "INV",
                            "port_directions": {"I": "input", "O": "output"},
                            "connections": {"I": [100], "O": [101]},
                        },
                    },
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "raw.json"
            output = Path(temporary) / "normalized.json"
            repeat = Path(temporary) / "normalized-repeat.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            report = normalize_xilinx_mapped_json(source, output, top="top")
            normalize_xilinx_mapped_json(source, repeat, top="top")
            self.assertEqual(output.read_bytes(), repeat.read_bytes())
            normalized = json.loads(output.read_text(encoding="utf-8"))
        cells = normalized["modules"]["top"]["cells"]
        self.assertEqual(report["carry4_input_cells"], 2)
        self.assertEqual(report["carry8_paired_cells"], 1)
        self.assertEqual(report["carry8_single_cells"], 0)
        self.assertEqual(report["inv_lowered_cells"], 1)
        self.assertEqual(cells["carry0"]["type"], "CARRY8")
        self.assertEqual(
            cells["carry0"]["parameters"]["CARRY_TYPE"], "SINGLE_CY8"
        )
        self.assertNotIn("carry1", cells)
        self.assertEqual(cells["inv"]["type"], "LUT1")
        self.assertEqual(cells["inv"]["parameters"]["INIT"], "01")
        self.assertEqual(report["primitive_audit"]["resource_totals"]["carry8"], 1)

    def test_unpaired_carry4_uses_one_dual_cy4_carry8(self) -> None:
        value = {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "carry": {
                            "type": "CARRY4",
                            "port_directions": {
                                "CI": "input",
                                "CYINIT": "input",
                                "DI": "input",
                                "S": "input",
                                "CO": "output",
                                "O": "output",
                            },
                            "connections": {
                                "CI": [2],
                                "CYINIT": [3],
                                "DI": [4, 5, 6, 7],
                                "S": [8, 9, 10, 11],
                                "CO": [12, 13, 14, 15],
                                "O": [16, 17, 18, 19],
                            },
                        }
                    },
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "raw.json"
            output = Path(temporary) / "normalized.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            report = normalize_xilinx_mapped_json(source, output, top="top")
            normalized = json.loads(output.read_text(encoding="utf-8"))
        cells = normalized["modules"]["top"]["cells"]
        self.assertEqual(report["carry8_single_cells"], 1)
        self.assertEqual(report["helper_lut_cells"], 1)
        self.assertEqual(
            cells["carry"]["parameters"]["CARRY_TYPE"], "DUAL_CY4"
        )
        self.assertIn("carry$cyinit_or", cells)

    def test_unused_carry_output_is_materialized_as_dead_carry8_nets(self) -> None:
        value = {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "carry": {
                            "type": "CARRY4",
                            "port_directions": {
                                "CI": "input", "CYINIT": "input",
                                "DI": "input", "S": "input", "CO": "output",
                            },
                            "connections": {
                                "CI": ["0"], "CYINIT": ["0"],
                                "DI": [1, 2, 3, 4], "S": [5, 6, 7, 8],
                                "CO": [9, 10, 11, 12],
                            },
                        }
                    },
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "raw.json"
            output = Path(temporary) / "normalized.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            normalize_xilinx_mapped_json(source, output, top="top")
            normalized = json.loads(output.read_text(encoding="utf-8"))
        carry = normalized["modules"]["top"]["cells"]["carry"]
        self.assertEqual(carry["type"], "CARRY8")
        self.assertEqual(len(carry["connections"]["O"]), 8)


if __name__ == "__main__":
    unittest.main()
