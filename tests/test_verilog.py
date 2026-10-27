import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from emuflow.common.io import write_json
from emuflow.common.errors import ValidationError
from emuflow.frontend.verilog import emit_mapped_verilog, mapped_verilog, write_mapped_verilog
from emuflow.frontend.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


class MappedVerilogTest(unittest.TestCase):
    def test_stream_failure_does_not_publish_partial_netlist(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"],
        )
        ir.value["ports"][0]["direction"] = "unknown"
        with TemporaryDirectory() as temp:
            output = Path(temp) / "mapped.v"
            output.write_bytes(b"previous valid output")
            with self.assertRaisesRegex(ValidationError, "unknown-direction"):
                write_mapped_verilog(ir, output)
            self.assertEqual(output.read_bytes(), b"previous valid output")
            self.assertEqual(list(Path(temp).iterdir()), [output])

    def test_streamed_writer_preserves_bytes_and_does_not_join_document(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"],
        )
        with TemporaryDirectory() as temp:
            for timing_only in (False, True):
                options = {
                    "timing_only": timing_only,
                    "timing_cell_types": {ir.value["instances"][0]["id"]: "special"},
                    "allow_bus_pins": not timing_only,
                    "synthesized_macro_types": {"LUT2"},
                }
                expected = mapped_verilog(ir, **options).encode()
                output = Path(temp) / "mapped.v"
                with patch(
                    "emuflow.frontend.verilog.mapped_verilog",
                    side_effect=AssertionError("whole-document string emission"),
                ):
                    write_mapped_verilog(ir, output, **options)
                self.assertEqual(output.read_bytes(), expected)

    def test_counter_emits_all_primitives_ports_and_constants(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        text = mapped_verilog(ir)
        self.assertIn("module \\counter ", text)
        self.assertEqual(text.count('(* KEEP = "yes"'), 8)
        self.assertIn("\\LUT2 ", text)
        self.assertIn("\\FDRE ", text)
        self.assertIn("4'b", text)
        self.assertIn("1'b", text)
        self.assertIn("endmodule", text)

    def test_unknown_primitive_init_is_omitted(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        instances = ir.value["instances"]
        ff = next(item for item in instances if item["type"].startswith("FD"))
        ff.setdefault("parameters", {})["INIT"] = "x"
        with TemporaryDirectory() as temp:
            ir_path = Path(temp) / "counter.emuir.json"
            output = Path(temp) / "mapped.v"
            report_path = Path(temp) / "report.json"
            write_json(ir_path, ir.value)
            report = emit_mapped_verilog(
                ir_path,
                output,
                report_path,
            )
            text = output.read_text(encoding="utf-8")
        self.assertNotIn(".\\INIT (1'bx)", text)
        self.assertEqual(report["omitted_unknown_init_parameters"], 1)


if __name__ == "__main__":
    unittest.main()
