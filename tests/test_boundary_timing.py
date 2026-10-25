import unittest

from emuflow.boundary_timing import (
    build_boundary_identity_database,
    build_boundary_timing_database,
    validate_boundary_identity_database,
    validate_boundary_timing_database,
)
from emuflow.ir import EmuIR


class BoundaryTimingIdentityTest(unittest.TestCase):
    def test_every_transport_endpoint_binds_to_a_merged_ir_port_net(self):
        transport = {
            "schema": "emuflow.transport-endpoints/v1",
            "design": "dut",
            "platform": "board",
            "fpga": "fpga0",
            "endpoints": [
                {
                    "id": "tx0",
                    "kind": "tx",
                    "schedule_entry": "s0",
                    "demand": "d0",
                    "net": "cut",
                    "link": "link-0",
                    "peer": "fpga1",
                    "lane": 0,
                    "logical_lane": 2,
                    "signal": "net:cut",
                },
                {
                    "id": "rx0",
                    "kind": "rx",
                    "schedule_entry": "s1",
                    "demand": "d1",
                    "net": "cut",
                    "link": "link-0",
                    "peer": "fpga1",
                    "lane": 1,
                    "logical_lane": 3,
                    "signal": "shadow:d1:fpga0",
                },
            ],
        }
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut__fpga0",
                    "top": "dut__fpga0",
                    "source_format": "test",
                },
                "ports": [
                    {
                        "id": "tx_link_0_fpga1",
                        "name": "tx_link_0_fpga1",
                        "direction": "output",
                        "width": 2,
                        "clock": False,
                        "reset": False,
                    },
                    {
                        "id": "rx_link_0_fpga1",
                        "name": "rx_link_0_fpga1",
                        "direction": "input",
                        "width": 2,
                        "clock": False,
                        "reset": False,
                    },
                ],
                "instances": [
                    {
                        "id": "__emuflow_transport__/shadow_ff",
                        "name": "__emuflow_transport__/shadow_ff",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                        "parameters": {"INIT": "0"},
                        "attributes": {},
                        "constant_connections": [],
                    }
                ],
                "nets": [
                    {
                        "id": "cut",
                        "name": "cut",
                        "drivers": [
                            {
                                "instance": "__emuflow_transport__/shadow_ff",
                                "port": "Q",
                                "bit": 0,
                            }
                        ],
                        "sinks": [],
                        "fanout": 0,
                        "cut_class": "register_output",
                    },
                    {
                        "id": "tx-wire",
                        "name": "tx-wire",
                        "drivers": [],
                        "sinks": [
                            {
                                "instance": None,
                                "port": "tx_link_0_fpga1",
                                "bit": 0,
                            }
                        ],
                        "fanout": 1,
                        "cut_class": "combinational",
                    },
                    {
                        "id": "rx-wire",
                        "name": "rx-wire",
                        "drivers": [
                            {
                                "instance": None,
                                "port": "rx_link_0_fpga1",
                                "bit": 1,
                            }
                        ],
                        "sinks": [],
                        "fanout": 0,
                        "cut_class": "combinational",
                    },
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        database = build_boundary_identity_database(transport, ir)
        checked = validate_boundary_identity_database(database, transport)
        self.assertEqual(checked["endpoints"], 2)
        by_id = {item["id"]: item for item in database["endpoints"]}
        self.assertEqual(by_id["tx0"]["merged_ir"]["external_net"], "tx-wire")
        self.assertEqual(by_id["rx0"]["merged_ir"]["external_net"], "rx-wire")
        self.assertEqual(
            by_id["rx0"]["merged_ir"]["boundary_register_instances"],
            ["__emuflow_transport__/shadow_ff"],
        )
        timing = build_boundary_timing_database(
            database,
            {
                "tx0": {
                    "delay_ns": 1.25,
                    "start_object": "cut",
                    "end_object": "tx_link_0_fpga1[0]",
                },
                "rx0": {
                    "delay_ns": 0.75,
                    "start_object": "rx_link_0_fpga1[1]",
                    "end_object": "shadow_ff/D",
                },
            },
            provider="test-timer",
            qualification="endpoint-exact",
        )
        timing_check = validate_boundary_timing_database(timing, database)
        self.assertEqual(timing_check["maximum_delay_ns"], 1.25)
        board_timing = build_boundary_timing_database(
            database,
            {
                "tx0": {
                    "delay_ns": 2.0,
                    "start_object": "launch/C",
                    "end_object": "tx-interface/D",
                },
                "rx0": {
                    "delay_ns": 1.5,
                    "start_object": "rx-interface/C",
                    "end_object": "shadow_ff/D",
                },
            },
            provider="board-timer",
            qualification="board-integrated",
            measurement_scope="board-integrated-interface",
        )
        validate_boundary_timing_database(board_timing, database)
        board_by_id = {
            item["id"]: item for item in board_timing["endpoints"]
        }
        self.assertEqual(
            board_by_id["tx0"]["measurement"],
            "routed-launch-through-tx-boundary-to-interface-endpoint",
        )
        word_timing=build_boundary_timing_database(database,{
            'tx0':{'delay_ns':2.0,'start_object':'launch/C','end_object':'serializer/D[0]'},
            'rx0':{'delay_ns':1.5,'start_object':'native-align/C','end_object':'shadow_ff/D'}},
            provider='word-timer',qualification='native-word',measurement_scope='native-word-registered-interface')
        validate_boundary_timing_database(word_timing,database)
        self.assertEqual({item['kind']:item['measurement']for item in word_timing['endpoints']},{
            'tx':'logical-source-to-native-tx-word-sampling-pin',
            'rx':'registered-native-rx-word-to-shadow-capture-within-fixed-arc'})

    def test_forwarded_endpoint_binds_synthesized_transport_shadow(self):
        transport = {
            "schema": "emuflow.transport-endpoints/v1",
            "design": "dut",
            "platform": "board",
            "fpga": "fpga0",
            "shadow_signals": [
                {"signal": "shadow:remote:fpga0", "index": 0}
            ],
            "endpoints": [
                {
                    "id": "tx-forward",
                    "kind": "tx",
                    "schedule_entry": "s0",
                    "demand": "d0",
                    "net": "remote-cut",
                    "link": "link-0",
                    "peer": "fpga1",
                    "lane": 0,
                    "logical_lane": 0,
                    "signal": "shadow:remote:fpga0",
                }
            ],
        }
        merged = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "dut__fpga0",
                    "top": "dut__fpga0",
                    "source_format": "test",
                },
                "ports": [
                    {
                        "id": "tx_link_0_fpga1",
                        "name": "tx_link_0_fpga1",
                        "direction": "output",
                        "width": 1,
                        "clock": False,
                        "reset": False,
                    }
                ],
                "instances": [
                    {
                        "id": "__emuflow_transport__/relay_ff",
                        "name": "__emuflow_transport__/relay_ff",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    }
                ],
                "nets": [
                    {
                        "id": "tx-wire",
                        "name": "tx-wire",
                        "drivers": [],
                        "sinks": [
                            {
                                "instance": None,
                                "port": "tx_link_0_fpga1",
                                "bit": 0,
                            }
                        ],
                        "fanout": 1,
                        "cut_class": "combinational",
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        synthesized_transport = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "transport",
                    "top": "transport",
                    "source_format": "test",
                },
                "ports": [
                    {
                        "id": "shadow_values",
                        "name": "shadow_values",
                        "direction": "output",
                        "width": 1,
                        "clock": False,
                        "reset": False,
                    }
                ],
                "instances": [
                    {
                        "id": "relay_ff",
                        "name": "relay_ff",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                        "parameters": {},
                        "attributes": {},
                        "constant_connections": [],
                    }
                ],
                "nets": [
                    {
                        "id": "shadow-net",
                        "name": "shadow-net",
                        "drivers": [
                            {"instance": "relay_ff", "port": "Q", "bit": 0}
                        ],
                        "sinks": [
                            {"instance": None, "port": "shadow_values", "bit": 0}
                        ],
                        "fanout": 1,
                        "cut_class": "register_output",
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        database = build_boundary_identity_database(
            transport, merged, synthesized_transport
        )
        validate_boundary_identity_database(database, transport)
        endpoint = database["endpoints"][0]
        self.assertEqual(endpoint["source_class"], "forwarded-shadow")
        self.assertIsNone(endpoint["merged_ir"]["logical_net"])
        self.assertEqual(
            endpoint["merged_ir"]["boundary_register_instances"],
            ["__emuflow_transport__/relay_ff"],
        )


if __name__ == "__main__":
    unittest.main()
