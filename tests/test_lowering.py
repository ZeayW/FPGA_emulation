import unittest
from copy import deepcopy

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.lowering import build_placement_ir


class PlacementIrLoweringTest(unittest.TestCase):
    @staticmethod
    def _empty_transport_ir(port):
        return EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "transport",
                    "top": "transport",
                    "source_format": "yosys-json",
                },
                "ports": [port],
                "instances": [],
                "nets": [],
                "clocks": [],
                "warnings": [],
            }
        )

    @staticmethod
    def _empty_netlist(port):
        return {
            "schema": "emuflow.fpga-netlist/v1",
            "design": {
                "name": "dut",
                "top": "dut",
                "source_format": "yosys-json",
            },
            "platform": "virtual",
            "fpga": "fpga0",
            "ports": [port],
            "instances": [],
            "nets": [],
            "resources": {},
        }

    @staticmethod
    def _empty_transport():
        return {
            "schema": "emuflow.transport-endpoints/v1",
            "design": "dut",
            "platform": "virtual",
            "fpga": "fpga0",
            "frame_slots": 8,
            "source_signals": [],
            "shadow_signals": [],
            "endpoints": [],
        }

    def test_compatible_reset_port_is_shared_with_transport(self) -> None:
        reset = {
            "id": "reset",
            "name": "reset",
            "direction": "input",
            "width": 1,
            "clock": False,
            "reset": True,
        }
        netlist = self._empty_netlist(reset)
        netlist["nets"] = [
            {
                "id": "reset@fpga0",
                "original_net": "reset_net",
                "name": "reset_net",
                "cut_class": "combinational",
                "drivers": [
                    {"instance": None, "port": "reset", "bit": 0}
                ],
                "sinks": [],
            }
        ]
        transport_ir = self._empty_transport_ir(reset)
        transport_ir.value["instances"] = [
            {
                "id": "transport_ff",
                "name": "transport_ff",
                "type": "FDRE",
                "resources": {"ff": 1},
                "parameters": {},
                "attributes": {},
                "constant_connections": [],
            }
        ]
        transport_ir.value["nets"] = [
            {
                "id": "transport_reset",
                "name": "transport_reset",
                "drivers": [
                    {"instance": None, "port": "reset", "bit": 0}
                ],
                "sinks": [
                    {"instance": "transport_ff", "port": "R", "bit": 0}
                ],
                "fanout": 1,
                "cut_class": "combinational",
            }
        ]
        result = build_placement_ir(
            netlist,
            self._empty_transport(),
            transport_ir,
        )
        self.assertEqual(
            [port["id"] for port in result.value["ports"]], ["reset"]
        )
        reset_net = next(
            net for net in result.value["nets"] if net["id"] == "reset_net"
        )
        self.assertEqual(
            reset_net["sinks"],
            [
                {
                    "instance": "__emuflow_transport__/transport_ff",
                    "port": "R",
                    "bit": 0,
                }
            ],
        )
        self.assertNotIn(
            "__emuflow_transport__/transport_reset",
            {net["id"] for net in result.value["nets"]},
        )

    def test_incompatible_dut_transport_port_collision_fails(self) -> None:
        dut_reset = {
            "id": "reset",
            "name": "reset",
            "direction": "input",
            "width": 1,
            "clock": False,
            "reset": True,
        }
        transport_reset = {**dut_reset, "width": 2}
        with self.assertRaisesRegex(ValidationError, "incompatible"):
            build_placement_ir(
                self._empty_netlist(dut_reset),
                self._empty_transport(),
                self._empty_transport_ir(transport_reset),
            )

    def test_shadow_output_is_stitched_to_original_remote_sinks(self) -> None:
        netlist = {
            "schema": "emuflow.fpga-netlist/v1",
            "design": {
                "name": "dut",
                "top": "dut",
                "source_format": "yosys-json",
            },
            "platform": "virtual",
            "fpga": "fpga0",
            "ports": [],
            "instances": [
                {
                    "id": "u_lut",
                    "name": "u_lut",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "constant_connections": [],
                }
            ],
            "nets": [
                {
                    "id": "cut@fpga0",
                    "original_net": "cut",
                    "name": "cut",
                    "cut_class": "register_output",
                    "source_kind": "transport_shadow",
                    "drivers": [
                        {
                            "instance": "__emuflow_rx_s000000",
                            "port": "shadow_out",
                            "bit": 0,
                        }
                    ],
                    "sinks": [
                        {"instance": "u_lut", "port": "I0", "bit": 0}
                    ],
                }
            ],
            "resources": {"lut": 1},
        }
        transport = {
            "schema": "emuflow.transport-endpoints/v1",
            "design": "dut",
            "platform": "virtual",
            "fpga": "fpga0",
            "frame_slots": 8,
            "source_signals": [],
            "shadow_signals": [
                {"index": 0, "signal": "shadow:d000000:fpga0"}
            ],
            "endpoints": [
                {
                    "id": "__emuflow_rx_s000000",
                    "kind": "rx",
                    "signal": "shadow:d000000:fpga0",
                    "net": "cut",
                }
            ],
        }
        transport_ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "transport",
                    "top": "transport",
                    "source_format": "yosys-json",
                },
                "ports": [
                    {
                        "id": "fabric_clk",
                        "name": "fabric_clk",
                        "direction": "input",
                        "width": 1,
                        "clock": True,
                        "reset": False,
                    },
                    {
                        "id": "shadow_values",
                        "name": "shadow_values",
                        "direction": "output",
                        "width": 1,
                        "clock": False,
                        "reset": False,
                    },
                    {
                        "id": "source_values",
                        "name": "source_values",
                        "direction": "input",
                        "width": 1,
                        "clock": False,
                        "reset": False,
                    },
                ],
                "instances": [
                    {
                        "id": "shadow_ff",
                        "name": "shadow_ff",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                        "parameters": {"INIT": "0"},
                        "attributes": {},
                        "constant_connections": [],
                    },
                    {
                        "id": "forward_mux",
                        "name": "forward_mux",
                        "type": "LUT1",
                        "resources": {"lut": 1},
                        "parameters": {"INIT": "10"},
                        "attributes": {},
                        "constant_connections": [],
                    },
                ],
                "nets": [
                    {
                        "id": "dummy_source",
                        "name": "source_values",
                        "drivers": [
                            {
                                "instance": None,
                                "port": "source_values",
                                "bit": 0,
                            }
                        ],
                        "sinks": [],
                        "fanout": 0,
                        "cut_class": "combinational",
                    },
                    {
                        "id": "q",
                        "name": "q",
                        "drivers": [
                            {"instance": "shadow_ff", "port": "Q", "bit": 0}
                        ],
                        "sinks": [
                            {
                                "instance": "forward_mux",
                                "port": "I0",
                                "bit": 0,
                            },
                            {
                                "instance": None,
                                "port": "shadow_values",
                                "bit": 0,
                            }
                        ],
                        "fanout": 1,
                        "cut_class": "register_output",
                    }
                ],
                "clocks": [
                    {
                        "id": "fabric_clk",
                        "name": "fabric_clk",
                        "source_port": "fabric_clk",
                        "period_ns": None,
                    }
                ],
                "warnings": [],
            }
        )
        class CountingNets(list):
            def __init__(self, values):
                super().__init__(values)
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        counting_nets = CountingNets(transport_ir.value["nets"])
        transport_ir.value["nets"] = counting_nets
        result = build_placement_ir(netlist, transport, transport_ir)
        # Building the top-port index and copying/consuming transport nets
        # requires three full passes, independent of the number of interface
        # bits. A per-bit full-net scan would make large TDM lowering O(P*N).
        self.assertEqual(counting_nets.iterations, 3)
        self.assertEqual(len(result.value["instances"]), 3)
        cut = next(net for net in result.value["nets"] if net["id"] == "cut")
        self.assertEqual(
            cut["drivers"][0]["instance"],
            "__emuflow_transport__/shadow_ff",
        )
        self.assertEqual(cut["sinks"][0]["instance"], "u_lut")
        self.assertIn(
            "__emuflow_transport__/forward_mux",
            {sink["instance"] for sink in cut["sinks"]},
        )
        self.assertNotIn(
            "shadow_values", {port["id"] for port in result.value["ports"]}
        )
        self.assertNotIn(
            "source_values", {port["id"] for port in result.value["ports"]}
        )
        self.assertNotIn(
            "dummy_source", {net["id"] for net in result.value["nets"]}
        )

        routing_netlist = deepcopy(netlist)
        routing_netlist["nets"] = []
        routing_only = build_placement_ir(
            routing_netlist, transport, transport_ir
        )
        forwarded = next(
            net
            for net in routing_only.value["nets"]
            if net["id"] == "__emuflow_transport__/q"
        )
        self.assertEqual(
            forwarded["drivers"][0]["instance"],
            "__emuflow_transport__/shadow_ff",
        )
        self.assertEqual(
            forwarded["sinks"][0]["instance"],
            "__emuflow_transport__/forward_mux",
        )
        self.assertTrue(
            all(
                endpoint["instance"] is not None
                for endpoint in forwarded["sinks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
