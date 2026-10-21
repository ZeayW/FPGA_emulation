import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.transport_fanout import buffer_transport_high_fanout_nets
from emuflow.xilinx_netlist import (
    emit_xilinx_mapped_index,
    open_xilinx_mapped_index,
)


def _transport_ir(fanout: int, *, cut_class: str = "combinational") -> EmuIR:
    instances = [
        {
            "id": "driver",
            "name": "driver",
            "type": "$lut",
            "resources": {"lut": 1},
            "parameters": {"LUT": "10", "WIDTH": "1"},
            "attributes": {},
            "constant_connections": [],
        }
    ]
    sinks = []
    for index in range(fanout):
        name = f"sink{index:04d}"
        instances.append(
            {
                "id": name,
                "name": name,
                "type": "$lut",
                "resources": {"lut": 1},
                "parameters": {"LUT": "10", "WIDTH": "1"},
                "attributes": {},
                "constant_connections": [],
            }
        )
        sinks.append({"instance": name, "port": "A", "bit": 0})
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "transport",
                "top": "transport",
                "source_format": "yosys-json",
            },
            "ports": [],
            "instances": instances,
            "nets": [
                {
                    "id": "wide",
                    "name": "wide",
                    "drivers": [
                        {"instance": "driver", "port": "Y", "bit": 0}
                    ],
                    "sinks": sinks,
                    "fanout": fanout,
                    "cut_class": cut_class,
                }
            ],
            "clocks": [],
            "warnings": [],
        }
    )


class TransportFanoutTest(unittest.TestCase):
    def test_inserts_two_level_tree_and_preserves_every_sink(self) -> None:
        ir = _transport_ir(17)
        expected_sinks = list(ir.value["nets"][0]["sinks"])

        report = buffer_transport_high_fanout_nets(ir, max_fanout=4)

        self.assertEqual(report["summary"]["buffered_nets"], 1)
        self.assertEqual(report["summary"]["buffer_cells"], 7)
        self.assertEqual(report["summary"]["maximum_result_fanout"], 4)
        self.assertEqual(report["nets"][0]["level_buffer_counts"], [5, 2])
        self.assertLessEqual(ir.value["nets"][0]["fanout"], 4)
        leaf_sinks = sorted(
            (
                endpoint
                for net in ir.value["nets"][1:6]
                for endpoint in net["sinks"]
            ),
            key=lambda endpoint: endpoint["instance"],
        )
        self.assertEqual(leaf_sinks, expected_sinks)
        for instance in ir.value["instances"][-7:]:
            self.assertEqual(instance["type"], "$lut")
            self.assertEqual(instance["parameters"]["LUT"], "10")
            self.assertEqual(instance["resources"]["lut"], 1)

    def test_leaves_clock_and_reset_nets_for_dedicated_handling(self) -> None:
        for cut_class in ("clock", "reset"):
            ir = _transport_ir(8, cut_class=cut_class)
            report = buffer_transport_high_fanout_nets(ir, max_fanout=4)
            self.assertEqual(report["summary"]["buffered_nets"], 0)
            self.assertEqual(len(ir.value["instances"]), 9)
            self.assertEqual(len(ir.value["nets"]), 1)

    def test_buffer_cells_stream_to_lut1_mapped_index(self) -> None:
        ir = _transport_ir(5)
        report = buffer_transport_high_fanout_nets(ir, max_fanout=4)
        self.assertEqual(report["summary"]["buffer_cells"], 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "transport.emuir.sqlite"
            mapped_path = root / "mapped.sqlite"
            ir.save(ir_path)
            emit_xilinx_mapped_index(ir_path, mapped_path)
            with open_xilinx_mapped_index(mapped_path) as mapped:
                module = mapped["modules"]["transport"]
                buffer_names = [
                    name
                    for name in module["cells"]
                    if name.startswith("$emuflow_transport_fanout$")
                ]
                self.assertEqual(len(buffer_names), 2)
                for name in buffer_names:
                    cell = module["cells"][name]
                    self.assertEqual(cell["type"], "LUT1")
                    self.assertEqual(cell["parameters"], {"INIT": "10"})

    def test_rejects_invalid_limit(self) -> None:
        with self.assertRaisesRegex(ValidationError, ">= 2"):
            buffer_transport_high_fanout_nets(_transport_ir(3), max_fanout=1)


if __name__ == "__main__":
    unittest.main()
