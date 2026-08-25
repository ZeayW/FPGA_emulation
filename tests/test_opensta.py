import json
import stat
import tempfile
import unittest
from pathlib import Path

from emuflow.opensta import (
    DEFAULT_TIMING_MODEL,
    FPGA_TIMING_MODEL_SCHEMA,
    FPGA_TIMING_MODEL_SCHEMA_V2,
    OPENSTA_PROVIDER,
    build_vtr_opensta_timing_model,
    classify_through_net_timing_endpoints,
    load_timing_model,
    parse_clock_definitions,
    render_opensta_liberty,
    run_opensta_path_database,
    validate_timing_model_coverage,
)
from emuflow.sta import validate_sta_path_database
from emuflow.verilog import mapped_verilog
from emuflow.vtr_architecture import run_vtr_architecture_import
from emuflow.yosys import import_yosys_json
from tests.native_build import vtr_architecture_importer


ROOT = Path(__file__).resolve().parents[1]
VTR_FIXTURE = (
    ROOT / "examples" / "architecture" / "vtr_k6_heterogeneous_fixture.xml"
)


class OpenStaProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )

    def test_open_model_is_explicitly_uncharacterized(self) -> None:
        model = load_timing_model(DEFAULT_TIMING_MODEL)
        self.assertEqual(model["schema"], FPGA_TIMING_MODEL_SCHEMA)
        self.assertEqual(
            model["source"]["qualification"],
            "analytical_uncharacterized",
        )
        coverage = validate_timing_model_coverage(self.ir, model)
        self.assertEqual(coverage["status"], "pass")
        liberty = render_opensta_liberty(model)
        self.assertIn("cell (LUT6)", liberty)
        self.assertIn("cell (FDRE)", liberty)
        self.assertIn("timing_type : setup_rising;", liberty)
        self.assertIn("timing_type : rising_edge;", liberty)
        timing_netlist = mapped_verilog(self.ir, timing_only=True)
        self.assertNotIn("input wire", timing_netlist)
        self.assertNotIn("KEEP =", timing_netlist)
        self.assertNotIn("#(", timing_netlist)

    def test_clock_definitions_are_strict(self) -> None:
        self.assertEqual(
            parse_clock_definitions(["clk=10", "aux=4.5"]),
            {"clk": 10.0, "aux": 4.5},
        )
        with self.assertRaisesRegex(Exception, "duplicate clock"):
            parse_clock_definitions(["clk=10", "clk=5"])
        with self.assertRaisesRegex(Exception, "expected CLOCK"):
            parse_clock_definitions(["clk"])

    def test_path_export_supports_directed_cut_net_queries(self) -> None:
        script = (
            ROOT / "scripts/opensta/export_timing_path_database.tcl"
        ).read_text(encoding="utf-8")
        self.assertIn("-group_count $max_paths", script)
        self.assertIn("EMUFLOW_STA_THROUGH_NETS", script)
        self.assertIn("get_pins -quiet -of_objects $through_net", script)
        self.assertIn("foreach through_pin $through_pins", script)
        self.assertIn("direction] ne \"output\"", script)
        self.assertIn("get_fanin -flat -startpoints_only", script)
        self.assertIn("get_fanout -flat -endpoints_only", script)
        self.assertIn("foreach startpoint $startpoints", script)
        self.assertIn("foreach endpoint $endpoints", script)
        self.assertIn(
            "-from [list $startpoint] -to [list $endpoint]", script
        )
        self.assertIn("$emitted == $before_emitted", script)
        self.assertIn("[info exists timed_endpoints($emuir_name)]", script)
        self.assertIn("-to $endpoint_pin", script)
        self.assertIn("-endpoint_count 1", script)
        self.assertIn("proc emuflow_emit_timing_paths", script)
        self.assertIn(
            '$required_net ne "" && ![info exists seen_net($required_net)]',
            script,
        )
        self.assertNotIn("forced_net", script)
        self.assertNotIn("forced_position", script)
        query = script.index("find_timing_paths -path_delay max", script.index("foreach line [lrange $through_lines"))
        emit = script.index("emuflow_emit_timing_paths", query)
        next_query = script.find("find_timing_paths -path_delay max", query + 1)
        self.assertLess(emit, next_query)

    def test_vtr_timing_db_builds_scalarized_opensta_model(self) -> None:
        source = {
            "creator": "OpenSTA VTR timing test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "clk": {"direction": "input", "bits": [2]},
                        "a": {"direction": "input", "bits": [3, 4]},
                        "q": {"direction": "output", "bits": [7]},
                    },
                    "cells": {
                        "lut": {
                            "type": "$lut",
                            "parameters": {"WIDTH": "10", "LUT": "0110"},
                            "port_directions": {
                                "A": "input",
                                "Y": "output",
                            },
                            "connections": {"A": [3, 4], "Y": [5]},
                        },
                        "ff": {
                            "type": "$_DFF_P_",
                            "parameters": {},
                            "port_directions": {
                                "C": "input",
                                "D": "input",
                                "Q": "output",
                            },
                            "connections": {"C": [2], "D": [5], "Q": [7]},
                        },
                    },
                    "netnames": {
                        "clk": {"bits": [2]},
                        "a": {"bits": [3, 4]},
                        "n": {"bits": [5]},
                        "q": {"bits": [7]},
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            yosys_path = root / "mapped.json"
            architecture_path = root / "architecture.json"
            timing_path = root / "timing.json"
            model_path = root / "model.json"
            yosys_path.write_text(json.dumps(source), encoding="utf-8")
            ir = import_yosys_json(yosys_path, top="top", clocks=["clk"])
            run_vtr_architecture_import(
                input_path=VTR_FIXTURE,
                architecture_output_path=architecture_path,
                timing_output_path=timing_path,
                architecture_id="fixture-k6",
                width=24,
                height=24,
                executable=str(vtr_architecture_importer()),
            )
            model, cell_types = build_vtr_opensta_timing_model(
                ir, timing_path, model_path
            )
            liberty = render_opensta_liberty(model)
            verilog = mapped_verilog(
                ir,
                timing_only=True,
                timing_cell_types=cell_types,
            )
        self.assertEqual(model["schema"], FPGA_TIMING_MODEL_SCHEMA_V2)
        self.assertEqual(
            model["source"]["qualification"], "academic_open_model"
        )
        self.assertGreater(
            model["source"]["sink_interconnect_delay_ns"], 0.0
        )
        self.assertIn("cell (EMUFLOW_VTR_LUT2)", liberty)
        self.assertIn("cell (EMUFLOW_VTR_DFF)", liberty)
        self.assertIn("pin (A__0)", liberty)
        self.assertIn(".\\A__1 ", verilog)

    def test_vtr_timing_db_normalizes_xilinx_lut_and_ff_names(self) -> None:
        source = {
            "creator": "OpenSTA Xilinx primitive normalization test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "clk": {"direction": "input", "bits": [2]},
                        "a": {"direction": "input", "bits": [3, 4, 5]},
                        "q": {"direction": "output", "bits": [9, 10]},
                    },
                    "cells": {
                        "lut": {
                            "type": "LUT3",
                            "parameters": {},
                            "port_directions": {
                                "I0": "input",
                                "I1": "input",
                                "I2": "input",
                                "O": "output",
                            },
                            "connections": {
                                "I0": [3],
                                "I1": [4],
                                "I2": [5],
                                "O": [6],
                            },
                        },
                        "ff_clear": {
                            "type": "FDCE",
                            "parameters": {},
                            "port_directions": {
                                "C": "input",
                                "CE": "input",
                                "CLR": "input",
                                "D": "input",
                                "Q": "output",
                            },
                            "connections": {
                                "C": [2],
                                "CE": ["1"],
                                "CLR": ["0"],
                                "D": [6],
                                "Q": [9],
                            },
                        },
                        "ff_reset": {
                            "type": "FDRE",
                            "parameters": {},
                            "port_directions": {
                                "C": "input",
                                "CE": "input",
                                "D": "input",
                                "Q": "output",
                                "R": "input",
                            },
                            "connections": {
                                "C": [2],
                                "CE": ["1"],
                                "D": [6],
                                "Q": [10],
                                "R": ["0"],
                            },
                        },
                    },
                    "netnames": {
                        "clk": {"bits": [2]},
                        "a": {"bits": [3, 4, 5]},
                        "n": {"bits": [6]},
                        "q": {"bits": [9, 10]},
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            yosys_path = root / "mapped.json"
            architecture_path = root / "architecture.json"
            timing_path = root / "timing.json"
            yosys_path.write_text(json.dumps(source), encoding="utf-8")
            ir = import_yosys_json(yosys_path, top="top", clocks=["clk"])
            run_vtr_architecture_import(
                input_path=VTR_FIXTURE,
                architecture_output_path=architecture_path,
                timing_output_path=timing_path,
                architecture_id="fixture-k6",
                width=24,
                height=24,
                executable=str(vtr_architecture_importer()),
            )
            model, cell_types = build_vtr_opensta_timing_model(ir, timing_path)
        self.assertEqual(cell_types["lut"], "EMUFLOW_VTR_LUT3")
        self.assertEqual(cell_types["ff_clear"], "EMUFLOW_VTR_FDCE")
        self.assertEqual(cell_types["ff_reset"], "EMUFLOW_VTR_FDRE")
        self.assertIn("EMUFLOW_VTR_LUT3", model["cells"])
        self.assertIn("EMUFLOW_VTR_FDCE", model["cells"])
        self.assertIn("EMUFLOW_VTR_FDRE", model["cells"])
        self.assertEqual(model["cells"]["EMUFLOW_VTR_FDCE"]["clock"], "C")

    def test_runner_imports_and_independently_checks_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            output_path = root / "database.json"
            log_path = root / "opensta.log"
            executable = root / "fake-openroad"
            ir_path.write_text(
                json.dumps(self.ir.value), encoding="utf-8"
            )
            executable.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path

through = Path(os.environ["EMUFLOW_STA_THROUGH_NETS"]).read_text().splitlines()
_, requested_hex = through[1].split("\\t")
path_id = "fake opensta path".encode().hex()
clock = "clk".encode().hex()
header = (
    "path_id_hex\\tclock_domain_hex\\tclock_period_ns\\t"
    "slack_ns\\tfixed_delay_ns\\tpath_nets_hex"
)
Path(os.environ["EMUFLOW_STA_OUTPUT"]).write_text(
    header + "\\n"
    + f"{path_id}\\t{clock}\\t10\\t9.5\\t0.5\\t{requested_hex}\\n"
)
Path(os.environ["EMUFLOW_STA_THROUGH_COVERAGE"]).write_text(
    "emuir_net_hex\\tdriver_count\\tqueried_paths\\temitted_paths\\n"
    + f"{requested_hex}\\t1\\t1\\t1\\n"
)
print("fake OpenSTA pass")
""",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            report = run_opensta_path_database(
                ir_path=ir_path,
                output_path=output_path,
                clocks={"clk": 10.0},
                executable=str(executable),
                max_paths=8,
                log_path=log_path,
                through_nets=[
                    next(
                        net["id"]
                        for net in self.ir.value["nets"]
                        if net["cut_class"] == "register_input"
                    )
                ],
            )
            checked = validate_sta_path_database(output_path, ir_path)
            artifact = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(report["provider"], OPENSTA_PROVIDER)
        self.assertEqual(
            report["timing_model_qualification"],
            "analytical_uncharacterized",
        )
        self.assertEqual(report["paths"], 1)
        self.assertEqual(report["max_paths"], 8)
        self.assertEqual(
            report["through_nets"],
            [
                next(
                    net["id"]
                    for net in self.ir.value["nets"]
                    if net["cut_class"] == "register_input"
                )
            ],
        )
        self.assertFalse(report["path_limit_reached"])
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(artifact["source"]["provider"], OPENSTA_PROVIDER)
        self.assertEqual(
            artifact["source"]["timing_model_qualification"],
            "analytical_uncharacterized",
        )

    def test_structural_endpoint_classifier_distinguishes_data_and_control(self) -> None:
        model = load_timing_model(DEFAULT_TIMING_MODEL)
        classified = classify_through_net_timing_endpoints(
            self.ir,
            model,
            [net["id"] for net in self.ir.value["nets"]],
        )
        by_class = {
            net["cut_class"]: classified[net["id"]]["status"]
            for net in self.ir.value["nets"]
        }
        self.assertEqual(by_class["register_input"], "timed")
        self.assertEqual(by_class["clock"], "no_timed_endpoint")
        self.assertEqual(by_class["reset"], "no_timed_endpoint")
        timed = next(
            net["id"]
            for net in self.ir.value["nets"]
            if net["cut_class"] == "register_input"
        )
        self.assertTrue(
            all(
                pin.endswith("/D")
                for pin in classified[timed]["direct_timed_endpoint_pins"]
            )
        )
        self.assertGreater(
            len(classified[timed]["direct_timed_endpoint_pins"]), 0
        )
        for net in self.ir.value["nets"]:
            if net["cut_class"] in {"clock", "reset"}:
                self.assertEqual(
                    classified[net["id"]]["direct_timed_endpoint_pins"], []
                )

    def test_structural_classifier_requires_sequential_startpoint(
        self,
    ) -> None:
        source = {
            "creator": "OpenSTA complete structural path test",
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "clk": {"direction": "input", "bits": [2]},
                        "a": {"direction": "input", "bits": [3]},
                        "q": {"direction": "output", "bits": [6]},
                    },
                    "cells": {
                        "ff0": {
                            "type": "FDRE",
                            "parameters": {},
                            "port_directions": {
                                "C": "input",
                                "CE": "input",
                                "D": "input",
                                "Q": "output",
                                "R": "input",
                            },
                            "connections": {
                                "C": [2],
                                "CE": ["1"],
                                "D": [3],
                                "Q": [4],
                                "R": ["0"],
                            },
                        },
                        "lut": {
                            "type": "LUT1",
                            "parameters": {},
                            "port_directions": {"I0": "input", "O": "output"},
                            "connections": {"I0": [4], "O": [5]},
                        },
                        "ff1": {
                            "type": "FDRE",
                            "parameters": {},
                            "port_directions": {
                                "C": "input",
                                "CE": "input",
                                "D": "input",
                                "Q": "output",
                                "R": "input",
                            },
                            "connections": {
                                "C": [2],
                                "CE": ["1"],
                                "D": [5],
                                "Q": [6],
                                "R": ["0"],
                            },
                        },
                    },
                    "netnames": {
                        "clk": {"bits": [2]},
                        "a": {"bits": [3]},
                        "q0": {"bits": [4]},
                        "stage": {"bits": [5]},
                        "q": {"bits": [6]},
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "mapped.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            ir = import_yosys_json(source_path, top="top", clocks=["clk"])
        model = load_timing_model(DEFAULT_TIMING_MODEL)
        classified = classify_through_net_timing_endpoints(
            ir,
            model,
            [net["id"] for net in ir.value["nets"]],
        )
        self.assertEqual(classified["a"]["direct_timed_endpoints"], 1)
        self.assertEqual(classified["a"]["status"], "no_timed_endpoint")
        self.assertEqual(classified["q0"]["status"], "timed")
        self.assertEqual(classified["stage"]["status"], "timed")
        self.assertEqual(classified["q"]["status"], "no_timed_endpoint")

    def test_runner_certifies_explicit_zero_path_control_net(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            output_path = root / "database.json"
            coverage_path = root / "coverage.json"
            executable = root / "fake-opensta"
            ir_path.write_text(json.dumps(self.ir.value), encoding="utf-8")
            executable.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path

requested = Path(os.environ["EMUFLOW_STA_THROUGH_NETS"]).read_text().splitlines()[1:]
requested_hex = [row.split("\\t")[1] for row in requested]
endpoints = Path(os.environ["EMUFLOW_STA_THROUGH_ENDPOINTS"]).read_text().splitlines()
assert endpoints[0] == "emuir_net_hex\\tendpoint_pin_hex"
assert any(row.split("\\t")[0] == requested_hex[0] for row in endpoints[1:])
assert all(row.split("\\t")[0] != requested_hex[1] for row in endpoints[1:])
header = (
    "path_id_hex\\tclock_domain_hex\\tclock_period_ns\\t"
    "slack_ns\\tfixed_delay_ns\\tpath_nets_hex"
)
Path(os.environ["EMUFLOW_STA_OUTPUT"]).write_text(
    header + "\\n" + "timed".encode().hex()
    + "\\t" + "clk".encode().hex()
    + f"\\t10\\t9.5\\t0.5\\t{requested_hex[0]}\\n"
)
Path(os.environ["EMUFLOW_STA_THROUGH_COVERAGE"]).write_text(
    "emuir_net_hex\\tdriver_count\\tqueried_paths\\temitted_paths\\n"
    + f"{requested_hex[0]}\\t1\\t1\\t1\\n"
    + f"{requested_hex[1]}\\t1\\t0\\t0\\n"
)
""",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            timed = next(
                net["id"]
                for net in self.ir.value["nets"]
                if net["cut_class"] == "register_input"
            )
            reset = next(
                net["id"]
                for net in self.ir.value["nets"]
                if net["cut_class"] == "reset"
            )
            report = run_opensta_path_database(
                ir_path,
                output_path,
                clocks={"clk": 10.0},
                executable=str(executable),
                through_nets=[timed, reset],
                through_coverage_path=coverage_path,
            )
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        self.assertEqual(report["covered_through_nets"], [timed])
        self.assertEqual(coverage["timed_nets"], 1)
        self.assertEqual(coverage["untimed_nets"], 1)
        self.assertEqual(
            coverage["records"][1]["structural"]["status"],
            "no_timed_endpoint",
        )

    def test_runner_rejects_zero_path_for_reachable_timed_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            output_path = root / "database.json"
            executable = root / "fake-opensta"
            ir_path.write_text(json.dumps(self.ir.value), encoding="utf-8")
            executable.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path

requested = Path(os.environ["EMUFLOW_STA_THROUGH_NETS"]).read_text().splitlines()[1:]
requested_hex = requested[0].split("\\t")[1]
other_hex = Path(os.environ["EMUFLOW_STA_NET_MAP"]).read_text().splitlines()[1].split("\\t")[1]
header = (
    "path_id_hex\\tclock_domain_hex\\tclock_period_ns\\t"
    "slack_ns\\tfixed_delay_ns\\tpath_nets_hex"
)
Path(os.environ["EMUFLOW_STA_OUTPUT"]).write_text(
    header + "\\n" + "other".encode().hex()
    + "\\t" + "clk".encode().hex()
    + f"\\t10\\t9.5\\t0.5\\t{other_hex}\\n"
)
Path(os.environ["EMUFLOW_STA_THROUGH_COVERAGE"]).write_text(
    "emuir_net_hex\\tdriver_count\\tqueried_paths\\temitted_paths\\n"
    + f"{requested_hex}\\t1\\t0\\t0\\n"
)
""",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            timed = next(
                net["id"]
                for net in self.ir.value["nets"]
                if net["cut_class"] == "register_input"
            )
            with self.assertRaisesRegex(Exception, "complete timed path"):
                run_opensta_path_database(
                    ir_path,
                    output_path,
                    clocks={"clk": 10.0},
                    executable=str(executable),
                    through_nets=[timed],
                )


if __name__ == "__main__":
    unittest.main()
