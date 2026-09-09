import copy
import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.board_arm_mps4 import materialize_arm_mps4_boarddb
from emuflow.board_support import BOARD_SUPPORT_OVERLAY_SCHEMA
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.physical_pins import (
    PACKAGE_PIN_BINDING_SCHEMA,
    SERIAL_TRANSCEIVER_PROVIDER,
    binding_to_xdc,
)
from emuflow.platform import Platform
from emuflow.serial_wrapper import (
    SERIAL_WRAPPER_SCHEMA,
    build_serial_wrapper_manifest,
    run_phase6c,
    serial_gt_site_tcl,
    serial_integration_shell_rtl,
    serial_wrapper_rtl,
)
from emuflow.runtime_sync import (
    build_runtime_sync_topology,
    validate_runtime_sync_provider,
)
from emuflow.vivado_pin_sites import (
    VIVADO_PIN_SITE_MAP_SCHEMA,
    collect_serial_pin_inventory,
    validate_lane_site_mapping,
)


class SerialWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.platform_path = root / "platform.json"
        materialize_arm_mps4_boarddb(
            self.platform_path,
            name="serial_wrapper_fixture",
            fabric_clock_mhz=50.0,
            payload_bits_per_lane_per_cycle=64,
            latency_cycles=4,
        )
        self.platform = Platform.load(self.platform_path)
        link = self.platform.links[0]
        source = link.endpoint_binding("mps4_1")
        sink = link.endpoint_binding("mps4_2")
        source_lane = source.lanes[0]
        sink_lane = sink.lanes[0]
        self.binding = {
            "schema": PACKAGE_PIN_BINDING_SCHEMA,
            "status": "source_backed_boarddb",
            "design": "serial_wrapper_fixture",
            "platform": self.platform.name,
            "board": self.platform.name,
            "provider": SERIAL_TRANSCEIVER_PROVIDER,
            "configuration": {
                "electrical_interface": "differential_serial_transceiver",
                "transceiver_site_status": "unresolved",
            },
            "metrics": {},
            "entries": [
                {
                    "id": "mps4_b2b_1:mps4_1-to-mps4_2:gty-0",
                    "link": "mps4_b2b_1",
                    "source": "mps4_1",
                    "sink": "mps4_2",
                    "physical_lane": 0,
                    "payload_bits_per_lane_per_cycle": 64,
                    "logical_lanes": [0, 1],
                    "logical_bindings": ["logical-0", "logical-1"],
                    "source_connector": source.connector,
                    "sink_connector": sink.connector,
                    "source_mgt_group": source.mgt,
                    "sink_mgt_group": sink.mgt,
                    "source_ports": {
                        "p": "gty_txp_mps4_b2b_1_mps4_2_lane0",
                        "n": "gty_txn_mps4_b2b_1_mps4_2_lane0",
                    },
                    "sink_ports": {
                        "p": "gty_rxp_mps4_b2b_1_mps4_1_lane0",
                        "n": "gty_rxn_mps4_b2b_1_mps4_1_lane0",
                    },
                    "source_package_pins": {
                        "p": source_lane.tx_package_pin_p,
                        "n": source_lane.tx_package_pin_n,
                    },
                    "sink_package_pins": {
                        "p": sink_lane.rx_package_pin_p,
                        "n": sink_lane.rx_package_pin_n,
                    },
                    "transceiver_site_status": "unresolved",
                }
            ],
        }
        self.transports = {
            fpga: {
                "schema": "emuflow.transport-endpoints/v1",
                "design": "serial_wrapper_fixture",
                "platform": self.platform.name,
                "fpga": fpga,
                "frame_slots": 4,
                "source_signals": (
                    [{"signal": "net:n0", "index": 0}]
                    if fpga == "mps4_1"
                    else []
                ),
                "shadow_signals": (
                    [{"signal": "shadow:d0:mps4_2", "index": 0}]
                    if fpga == "mps4_2"
                    else []
                ),
                "endpoints": (
                    [
                        {
                            "id": "__emuflow_tx_s0",
                            "kind": "tx",
                            "link": "mps4_b2b_1",
                            "peer": "mps4_2",
                        }
                    ]
                    if fpga == "mps4_1"
                    else [
                        {
                            "id": "__emuflow_rx_s0",
                            "kind": "rx",
                            "link": "mps4_b2b_1",
                            "peer": "mps4_1",
                        }
                    ]
                    if fpga == "mps4_2"
                    else []
                ),
            }
            for fpga in ("mps4_1", "mps4_2", "mps4_3")
        }
        self.overlay = {
            "schema": BOARD_SUPPORT_OVERLAY_SCHEMA,
            "platform": self.platform.name,
            "qualification": "source_backed_hardware_definition",
            "provenance": {
                "sources": [
                    {
                        "title": "Unit-test board definition",
                        "uri": "https://example.invalid/unit-test-board",
                        "locator": "synthetic test fixture only",
                    }
                ]
            },
            "reference_clocks": [
                {
                    "id": f"{fpga}_refclk0",
                    "fpga": fpga,
                    "board_service": "b2b_mgt_refclk_pool",
                    "selected_signal": "B2B_CLK[0]",
                    "package_pins": {
                        "p": f"{fpga.upper()}_REFP",
                        "n": f"{fpga.upper()}_REFN",
                    },
                    "frequency_mhz": 156.25,
                    "frequency_basis": "documented",
                }
                for fpga in ("mps4_1", "mps4_2")
            ],
            "resets": [
                {
                    "id": f"{fpga}_reset",
                    "fpga": fpga,
                    "board_service": "cb_npor",
                    "package_pin": f"{fpga.upper()}_RST",
                    "iostandard": "LVCMOS18",
                }
                for fpga in ("mps4_1", "mps4_2")
            ],
            "transceiver_sites": [
                {
                    "fpga": fpga,
                    "link": "mps4_b2b_1",
                    "connector": connector,
                    "mgt_group": mgt,
                    "physical_lane": 0,
                    "site": site,
                    "reference_clock_binding": f"{fpga}_refclk0",
                    "reset_binding": f"{fpga}_reset",
                }
                for fpga, connector, mgt, site in (
                    ("mps4_1", "J49", "MGT0", "GTYE4_CHANNEL_X0Y0"),
                    ("mps4_2", "J48", "MGT1", "GTYE4_CHANNEL_X0Y1"),
                )
            ],
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_vendor_gt_tcl_resolves_generated_descendants_per_quad(self) -> None:
        tcl = serial_gt_site_tcl(
            {
                "transceiver_quads": [
                    {
                        "common_site": "GTYE4_COMMON_X0Y1",
                        "channels": [
                            {
                                "channel_index": 1,
                                "channel_site": "GTYE4_CHANNEL_X0Y5",
                            },
                            {
                                "channel_index": 3,
                                "channel_site": "GTYE4_CHANNEL_X0Y7",
                            },
                        ],
                    }
                ]
            },
            {
                "channel_instance_template": "channel_gen[{channel}].adapter",
                "channel_primitive": "GTYE4_CHANNEL",
                "common_primitive": "GTYE4_COMMON",
                "hierarchy_resolution": "descendant_primitive_sorted",
            },
        )
        self.assertIn("REF_NAME == GTYE4_CHANNEL", tcl)
        self.assertIn("GTYE4_CHANNEL_X0Y5 GTYE4_CHANNEL_X0Y7", tcl)
        self.assertIn("REF_NAME == GTYE4_COMMON", tcl)
        self.assertIn("set_property LOC GTYE4_COMMON_X0Y1", tcl)

    def _write_phy_provider(
        self, qualification: str, schema: str = "emuflow.serial-phy-provider/v1"
    ) -> Path:
        root = Path(self.temporary_directory.name)
        suffix = "v2" if schema.endswith("/v2") else "v1"
        source = root / f"provider-{qualification}-{suffix}.sv"
        lane_or_quad = (
            """module emuflow_external_serial_phy_quad #(
  parameter integer PAYLOAD_WIDTH = 64,
  parameter [3:0] ACTIVE_CHANNEL_MASK = 4'b0000
) (input wire user_clk, input wire reset, input wire phy_refclk,
   input wire phy_reset, input wire [4*PAYLOAD_WIDTH-1:0] tx_data,
   output wire [4*PAYLOAD_WIDTH-1:0] rx_data,
   output wire [3:0] txp, output wire [3:0] txn,
   input wire [3:0] rxp, input wire [3:0] rxn,
   output wire [3:0] lane_ready, output wire common_ready);
  assign rx_data = {4*PAYLOAD_WIDTH{1'b0}};
  assign txp = 4'b0000; assign txn = 4'b1111;
  assign lane_ready = ACTIVE_CHANNEL_MASK; assign common_ready = 1'b1;
endmodule
"""
            if schema.endswith("/v2")
            else """module emuflow_external_serial_phy_lane #(
  parameter integer PAYLOAD_WIDTH = 64
) (input wire user_clk, input wire reset, input wire phy_refclk,
   input wire phy_reset, input wire [PAYLOAD_WIDTH-1:0] tx_data,
   output wire [PAYLOAD_WIDTH-1:0] rx_data, output wire txp,
   output wire txn, input wire rxp, input wire rxn, output wire ready);
  assign rx_data = {PAYLOAD_WIDTH{1'b0}};
  assign txp = 1'b0; assign txn = 1'b1; assign ready = 1'b1;
endmodule
"""
        )
        source_text = """module emuflow_external_serial_clock_reset #(
  parameter integer BOARD_RESET_ACTIVE_LOW = 1
) (input wire refclk_p, input wire refclk_n, input wire board_reset,
   output wire phy_refclk, output wire phy_reset, output wire ready);
  assign phy_refclk = refclk_p;
  assign phy_reset = BOARD_RESET_ACTIVE_LOW ? ~board_reset : board_reset;
  assign ready = 1'b1;
endmodule
""" + lane_or_quad
        if qualification == "editable_source_hardware":
            source_text = source_text.replace(
                "  assign phy_refclk = refclk_p;",
                "  IBUFDS_GTE4 refclk_buffer ();\n"
                "  assign phy_refclk = refclk_p;",
            )
            source_text = source_text.replace(
                "  assign rx_data =",
                (
                    "  GTYE4_COMMON gty_common ();\n"
                    "  GTYE4_CHANNEL gty_channel ();\n"
                    if schema.endswith("/v2")
                    else "  GTYE4_CHANNEL gty_channel ();\n"
                )
                + "  assign rx_data =",
            )
        source.write_text(source_text, encoding="utf-8")
        manifest = root / f"provider-{qualification}-{suffix}.json"
        write_json(
            manifest,
            {
                "schema": schema,
                "id": f"{qualification}_fixture",
                "qualification": qualification,
                "supported_parts": ["xcvu13p-fhga2104-1-e"],
                "modules": (
                    {
                        "clock_reset": "emuflow_external_serial_clock_reset",
                        "quad": "emuflow_external_serial_phy_quad",
                    }
                    if schema.endswith("/v2")
                    else {
                        "clock_reset": "emuflow_external_serial_clock_reset",
                        "lane": "emuflow_external_serial_phy_lane",
                    }
                ),
                "implementation": (
                    {
                        "kind": "amd_ultrascale_plus_gty",
                        "channel_primitive": "GTYE4_CHANNEL",
                        **(
                            {
                                "common_primitive": "GTYE4_COMMON",
                                "channel_instance_template": (
                                    "channel_{channel}.gty_channel"
                                ),
                                "common_instance": "gty_common",
                            }
                            if schema.endswith("/v2")
                            else {"channel_instance": "gty_channel"}
                        ),
                        "reference_clock_primitive": "IBUFDS_GTE4",
                        "reference_clock_instance": "refclk_buffer",
                    }
                    if qualification == "editable_source_hardware"
                    else {"kind": "behavioral"}
                ),
                "source_root": ".",
                "sources": [
                    {
                        "path": source.name,
                        "language": "systemverilog",
                        "role": "unit_test_fixture",
                        "sha256": hashlib.sha256(
                            source_text.encode("utf-8")
                        ).hexdigest(),
                    }
                ],
                "protocol": {
                    "payload_bits_per_lane_per_cycle": 64,
                    "user_clock_mhz": 50.0,
                    "line_rate_gbps_per_lane": 10.0,
                    "encoding": "unit_test",
                    "link_training": "unit_test",
                    "reset_sequence": "unit_test",
                },
                "provenance": {
                    "license": "unit-test-only",
                    "upstream": "repository unit test",
                },
            },
        )
        return manifest

    def _gt_site_map(self):
        pins_by_part, lanes = collect_serial_pin_inventory(self.platform)
        rows = {}
        function_stem = {
            "tx_p": "MGTYTXP",
            "tx_n": "MGTYTXN",
            "rx_p": "MGTYRXP",
            "rx_n": "MGTYRXN",
        }
        for lane in lanes:
            offset = 20 if lane["connector"] == "J49" else 36
            channel_y = offset + lane["physical_lane"]
            site = f"GTYE4_CHANNEL_X0Y{channel_y}"
            common_site = f"GTYE4_COMMON_X0Y{channel_y // 4}"
            for role, pin in lane["package_pins"].items():
                rows.setdefault(
                    pin,
                    {
                        "pin_function": (
                            f"{function_stem[role]}"
                            f"{lane['physical_lane'] % 4}_129"
                        ),
                        "site": site,
                        "common_site": common_site,
                    },
                )
        part = next(iter(pins_by_part))
        return {
            "schema": VIVADO_PIN_SITE_MAP_SCHEMA,
            "status": "pass",
            "qualification": (
                "vendor_device_db_derived_from_source_backed_package_pins"
            ),
            "platform": self.platform.name,
            "platform_sha256": hashlib.sha256(
                self.platform_path.read_bytes()
            ).hexdigest(),
            "transceiver_sites": validate_lane_site_mapping(
                lanes, {part: rows}
            ),
        }

    def _write_v3_phy_provider(self) -> Path:
        root = Path(self.temporary_directory.name)
        source = root / "provider-simulation-v3.sv"
        source_text = """module emuflow_external_serial_clock_reset #(
  parameter integer BOARD_RESET_ACTIVE_LOW = 1
) (input wire refclk_p, input wire refclk_n, input wire board_reset,
   output wire phy_refclk, output wire phy_reset, output wire ready);
  assign phy_refclk = refclk_p; assign phy_reset = board_reset;
  assign ready = 1'b1;
endmodule
module emuflow_external_gty_serdes_quad #(
  parameter [3:0] ACTIVE_CHANNEL_MASK = 4'b0000
) (input wire phy_refclk, input wire phy_reset,
   input wire [255:0] serdes_tx_data, input wire [7:0] serdes_tx_hdr,
   output wire [255:0] serdes_rx_data, output wire [7:0] serdes_rx_hdr,
   input wire [3:0] serdes_rx_bitslip, input wire [3:0] serdes_rx_reset_req,
   output wire [3:0] tx_usrclk, output wire [3:0] rx_usrclk,
   output wire [3:0] txp, output wire [3:0] txn,
   input wire [3:0] rxp, input wire [3:0] rxn,
   output wire [3:0] lane_ready, output wire common_ready);
  assign serdes_rx_data = 256'b0; assign serdes_rx_hdr = 8'b0;
  assign tx_usrclk = {4{phy_refclk}}; assign rx_usrclk = {4{phy_refclk}};
  assign txp = 4'b0; assign txn = 4'b1;
  assign lane_ready = ACTIVE_CHANNEL_MASK; assign common_ready = 1'b1;
endmodule
"""
        source.write_text(source_text, encoding="utf-8")
        manifest = root / "provider-simulation-v3.json"
        write_json(
            manifest,
            {
                "schema": "emuflow.serial-phy-provider/v3",
                "id": "simulation_v3_fixture",
                "qualification": "simulation_only",
                "supported_parts": ["xcvu13p-fhga2104-1-e"],
                "modules": {
                    "clock_reset": "emuflow_external_serial_clock_reset",
                    "serdes_quad": "emuflow_external_gty_serdes_quad",
                },
                "implementation": {"kind": "behavioral"},
                "source_root": ".",
                "sources": [
                    {
                        "path": source.name,
                        "language": "systemverilog",
                        "role": "parallel_66b_serdes_fixture",
                        "sha256": hashlib.sha256(
                            source_text.encode("utf-8")
                        ).hexdigest(),
                    }
                ],
                "protocol": {
                    "payload_bits_per_lane_per_cycle": 64,
                    "user_clock_mhz": 50.0,
                    "line_rate_gbps_per_lane": 10.3125,
                    "encoding": "64b66b",
                    "link_training": "corundum_block_lock",
                    "reset_sequence": "provider_defined",
                    "pcs_data_width": 64,
                    "pcs_header_width": 2,
                    "pcs_clock_mhz": 156.25,
                    "pcs_implementation": (
                        "emuflow-in-tree-corundum-10gbase-r"
                    ),
                },
                "provenance": {
                    "license": "unit-test-only",
                    "upstream": "repository unit test",
                },
            },
        )
        return manifest

    def test_wrapper_is_reproducible_and_exposes_exact_active_ports(self) -> None:
        first = build_serial_wrapper_manifest(self.platform, self.binding)
        second = build_serial_wrapper_manifest(self.platform, self.binding)
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], SERIAL_WRAPPER_SCHEMA)
        self.assertEqual(first["status"], "awaiting_external_phy_provider")
        self.assertEqual(first["metrics"]["active_transceiver_sites"], 2)
        phy_contract = first["phy_contract"]
        self.assertEqual(
            phy_contract["internal_reset"]["derivation_status"],
            "unresolved_from_board_reset",
        )
        self.assertEqual(
            phy_contract["board_service_candidates"]["reference_clocks"][0][
                "frequency_mhz"
            ],
            156.25,
        )
        self.assertEqual(
            {
                reset["signal"]
                for reset in phy_contract["board_service_candidates"]["resets"]
            },
            {"IOFPGA_nRST", "CB_nPOR"},
        )
        self.assertIn(
            "reference_clock_package_binding",
            phy_contract["required_provider_fields"],
        )
        source = next(
            item for item in first["fpgas"] if item["fpga"] == "mps4_1"
        )
        connection = next(
            item
            for item in source["transport_connections"]
            if item["link"] == "mps4_b2b_1"
        )
        self.assertEqual(connection["width"], 768)
        self.assertEqual(
            connection["transport_tx_port"], "tx_mps4_b2b_1_mps4_2"
        )
        rtl = serial_wrapper_rtl(
            self.platform, "mps4_1", source["sites"]
        )
        self.assertIn("module emuflow_serial_wrapper_mps4_1", rtl)
        self.assertIn("gty_txp_mps4_b2b_1_mps4_2_lane0", rtl)
        self.assertIn("tx_mps4_b2b_1_mps4_2[0 +: 64]", rtl)
        self.assertIn("emuflow_external_serial_phy_lane", rtl)
        self.assertIn(".phy_refclk(1'b0)", rtl)
        self.assertIn(".phy_reset(reset)", rtl)
        self.assertNotIn("GTYE4_CHANNEL", rtl)
        xdc_ports = set(
            re.findall(r"\[get_ports \{([^}]+)\}\]", binding_to_xdc(
                self.binding, "mps4_1"
            ))
        )
        self.assertEqual(
            xdc_ports,
            {"gty_txp_mps4_b2b_1_mps4_2_lane0",
             "gty_txn_mps4_b2b_1_mps4_2_lane0"},
        )
        self.assertTrue(all(port in rtl for port in xdc_ports))
        shell = serial_integration_shell_rtl(
            self.platform,
            "mps4_1",
            source["sites"],
            self.transports["mps4_1"],
        )
        self.assertIn("module emuflow_partition_shell_mps4_1", shell)
        self.assertIn("emuflow_transport_mps4_1 transport", shell)
        self.assertIn(".tx_mps4_b2b_1_mps4_2(", shell)
        self.assertIn(".links_ready(links_ready)", shell)

    def test_overlay_controls_survive_manifest_and_integration_shell(self):
        overlay = copy.deepcopy(self.overlay)
        overlay["static_outputs"] = [{"id": "qsfp_enable", "fpga": "mps4_1",
            "package_pin": "ZZ99", "iostandard": "LVCMOS18", "value": 1}]
        manifest = build_serial_wrapper_manifest(self.platform, self.binding,
                                                  board_overlay=overlay)
        source = next(item for item in manifest["fpgas"] if item["fpga"] == "mps4_1")
        self.assertEqual(source["board_services"]["static_outputs"], overlay["static_outputs"])
        shell = serial_integration_shell_rtl(self.platform, "mps4_1", source["sites"],
            self.transports["mps4_1"], board_services=source["board_services"])
        self.assertIn("output wire board_static_qsfp_enable", shell)
        self.assertIn("assign board_static_qsfp_enable = 1'b1;", shell)

    def test_boarddb_pin_corruption_is_rejected(self) -> None:
        corrupted = copy.deepcopy(self.binding)
        corrupted["entries"][0]["source_package_pins"]["p"] = "WRONG"
        with self.assertRaisesRegex(ValidationError, "disagrees with BoardDB"):
            build_serial_wrapper_manifest(self.platform, corrupted)

    def test_phase6c_writes_checked_black_box_boundary(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding.json"
        output = root / "phase6c"
        write_json(binding_path, self.binding)
        report = run_phase6c(self.platform_path, binding_path, output)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["hardware_release_status"],
            "blocked_on_external_phy_provider",
        )
        manifest = read_json(output / "serial_wrapper_manifest.json")
        self.assertEqual(
            manifest["phy_contract"]["implementation_status"],
            "black_box_unresolved",
        )
        self.assertEqual(
            manifest["phy_contract"]["board_service_candidates"]
            ["reference_clocks"][0]["signal"],
            "B2B_CLK[9:0]",
        )
        contract = (output / "external_serial_phy_contract.sv").read_text()
        self.assertIn("(* black_box *)", contract)
        self.assertIn("emuflow_external_serial_clock_reset", contract)

    def test_phase6c_integrates_exact_transport_port_directions(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-integrated.json"
        output = root / "phase6c-integrated"
        write_json(binding_path, self.binding)
        transport_paths = {}
        for fpga, transport in self.transports.items():
            path = root / f"{fpga}.transport.json"
            write_json(path, transport)
            transport_paths[fpga] = path
        report = run_phase6c(
            self.platform_path,
            binding_path,
            output,
            transport_paths=transport_paths,
        )
        self.assertEqual(
            report["validation"]["integrated_transport_shells"], 3
        )
        self.assertIn("integration_shells", report["artifacts"])
        source_shell = (
            output / "mps4_1.serial_integration_shell.sv"
        ).read_text()
        sink_shell = (
            output / "mps4_2.serial_integration_shell.sv"
        ).read_text()
        source_transport = source_shell.split(
            "emuflow_transport_mps4_1 transport", 1
        )[1].split("emuflow_serial_wrapper_mps4_1", 1)[0]
        sink_transport = sink_shell.split(
            "emuflow_transport_mps4_2 transport", 1
        )[1].split("emuflow_serial_wrapper_mps4_2", 1)[0]
        self.assertIn(".tx_mps4_b2b_1_mps4_2(", source_transport)
        self.assertNotIn(".rx_mps4_b2b_1_mps4_2(", source_transport)
        self.assertIn(".rx_mps4_b2b_1_mps4_1(", sink_transport)
        self.assertNotIn(".tx_mps4_b2b_1_mps4_1(", sink_transport)

    def test_phase6c_integrates_source_visible_runtime_sync_node(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-runtime-sync.json"
        topology_path = root / "runtime-sync-topology.json"
        output = root / "phase6c-runtime-sync"
        write_json(binding_path, self.binding)
        transport_paths = {}
        for fpga, transport in self.transports.items():
            path = root / f"{fpga}.runtime-sync.transport.json"
            write_json(path, transport)
            transport_paths[fpga] = path
        provider_path = (
            Path(__file__).resolve().parents[1]
            / "providers/runtime_sync_tree/provider.json"
        )
        provider = validate_runtime_sync_provider(
            read_json(provider_path), provider_path
        )["normalized"]
        topology = build_runtime_sync_topology(self.platform, provider)
        write_json(topology_path, topology)
        report = run_phase6c(
            self.platform_path,
            binding_path,
            output,
            transport_paths=transport_paths,
            runtime_sync_topology_path=topology_path,
            runtime_sync_provider_path=provider_path,
        )
        self.assertEqual(report["validation"]["runtime_sync_nodes"], 3)
        manifest = read_json(output / "serial_wrapper_manifest.json")
        required = manifest["phy_contract"]["required_provider_fields"]
        self.assertNotIn("global_ready_consensus", required)
        self.assertIn("runtime_sync_control_transport", required)
        shell = (output / "mps4_1.serial_integration_shell.sv").read_text()
        self.assertIn("emuflow_runtime_sync_tree_node", shell)
        self.assertIn(".local_ready(local_links_ready)", shell)
        self.assertIn(".global_ready(links_ready)", shell)
        self.assertIn(".links_ready(local_links_ready)", shell)
        self.assertIn("runtime_sync_rtl", report["artifacts"])

    def test_source_backed_overlay_resolves_site_and_refclk_data_only(self) -> None:
        manifest = build_serial_wrapper_manifest(
            self.platform, self.binding, board_overlay=self.overlay
        )
        self.assertEqual(
            manifest["metrics"]["source_backed_resolved_transceiver_sites"], 2
        )
        self.assertEqual(manifest["metrics"]["unresolved_transceiver_sites"], 0)
        self.assertNotIn(
            "transceiver_site",
            manifest["phy_contract"]["required_provider_fields"],
        )
        self.assertIn(
            "reset_synchronization",
            manifest["phy_contract"]["required_provider_fields"],
        )
        source = next(
            item for item in manifest["fpgas"] if item["fpga"] == "mps4_1"
        )
        self.assertEqual(
            source["sites"][0]["transceiver_site_status"],
            "resolved_source_backed",
        )
        self.assertEqual(
            source["sites"][0]["transceiver_site"],
            "GTYE4_CHANNEL_X0Y0",
        )
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-overlay.json"
        overlay_path = root / "overlay.json"
        output = root / "phase6c-overlay"
        write_json(binding_path, self.binding)
        write_json(overlay_path, self.overlay)
        report = run_phase6c(
            self.platform_path,
            binding_path,
            output,
            board_overlay_path=overlay_path,
        )
        self.assertEqual(
            report["hardware_release_status"],
            "blocked_on_external_phy_provider",
        )
        written = read_json(output / "serial_wrapper_manifest.json")
        self.assertEqual(len(written["board_overlay_sha256"]), 64)
        service_xdc = (output / "mps4_1.board_services.xdc").read_text()
        self.assertIn("set_property PACKAGE_PIN MPS4_1_REFP", service_xdc)
        self.assertIn("create_clock -name mps4_1_refclk0 -period 6.4", service_xdc)
        self.assertIn("set_property IOSTANDARD LVCMOS18", service_xdc)
        wrapper = (output / "mps4_1.serial_wrapper.sv").read_text()
        self.assertIn("input  wire refclk_mps4_1_refclk0_p", wrapper)
        self.assertIn("input  wire board_reset_mps4_1_reset", wrapper)
        self.assertIn("emuflow_external_serial_clock_reset", wrapper)

    def test_unverified_overlay_does_not_reduce_hardware_gaps(self) -> None:
        overlay = copy.deepcopy(self.overlay)
        overlay["qualification"] = "user_supplied_unverified"
        manifest = build_serial_wrapper_manifest(
            self.platform, self.binding, board_overlay=overlay
        )
        self.assertEqual(
            manifest["metrics"]["overlay_bound_transceiver_sites"], 2
        )
        self.assertEqual(
            manifest["metrics"]["source_backed_resolved_transceiver_sites"],
            0,
        )
        self.assertEqual(manifest["metrics"]["unresolved_transceiver_sites"], 2)
        self.assertIn(
            "transceiver_site",
            manifest["phy_contract"]["required_provider_fields"],
        )

    def test_phase6c_binds_provider_source_without_overclaiming_release(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-provider.json"
        overlay_path = root / "overlay-provider.json"
        write_json(binding_path, self.binding)
        write_json(overlay_path, self.overlay)

        simulation_provider = self._write_phy_provider("simulation_only")
        simulation_out = root / "phase6c-simulation-provider"
        simulation_report = run_phase6c(
            self.platform_path,
            binding_path,
            simulation_out,
            board_overlay_path=overlay_path,
            phy_provider_path=simulation_provider,
        )
        self.assertEqual(
            simulation_report["hardware_release_status"],
            "blocked_on_external_phy_provider",
        )
        simulation_manifest = read_json(
            simulation_out / "serial_wrapper_manifest.json"
        )
        self.assertEqual(simulation_manifest["status"], "provider_source_bound")
        self.assertIn(
            "reset_synchronization",
            simulation_manifest["phy_contract"]["required_provider_fields"],
        )

        hardware_provider = self._write_phy_provider(
            "editable_source_hardware"
        )
        hardware_out = root / "phase6c-hardware-provider"
        hardware_report = run_phase6c(
            self.platform_path,
            binding_path,
            hardware_out,
            board_overlay_path=overlay_path,
            phy_provider_path=hardware_provider,
        )
        self.assertEqual(
            hardware_report["hardware_release_status"],
            "blocked_on_external_phy_provider",
        )
        hardware_manifest = read_json(
            hardware_out / "serial_wrapper_manifest.json"
        )
        self.assertIn(
            "quad_shared_common",
            hardware_manifest["phy_contract"]["required_provider_fields"],
        )
        self.assertEqual(
            hardware_manifest["metrics"]["provider_source_bound_phy_modules"],
            2,
        )
        self.assertEqual(len(hardware_manifest["phy_provider_manifest_sha256"]), 64)
        self.assertTrue(
            (hardware_out / "serial_phy_provider.normalized.json").is_file()
        )
        self.assertEqual(
            hardware_report["artifacts"]["gt_site_tcl"]["mps4_1"],
            "mps4_1.gt_sites.tcl",
        )
        gt_tcl = (hardware_out / "mps4_1.gt_sites.tcl").read_text()
        self.assertIn(
            "set_property LOC GTYE4_CHANNEL_X0Y0 "
            "[get_cells {serial_wrapper/site_0_phy/gty_channel}]",
            gt_tcl,
        )

    def test_vivado_site_map_emits_real_loc_but_keeps_services_blocked(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-gt-map.json"
        gt_site_map_path = root / "gt-site-map.json"
        output = root / "phase6c-gt-map"
        write_json(binding_path, self.binding)
        write_json(gt_site_map_path, self._gt_site_map())
        report = run_phase6c(
            self.platform_path,
            binding_path,
            output,
            phy_provider_path=self._write_phy_provider(
                "editable_source_hardware"
            ),
            gt_site_map_path=gt_site_map_path,
        )
        self.assertEqual(
            report["hardware_release_status"],
            "blocked_on_external_phy_provider",
        )
        self.assertEqual(
            report["validation"]["vendor_derived_transceiver_sites"], 2
        )
        self.assertEqual(report["validation"]["active_transceiver_quads"], 2)
        self.assertEqual(report["validation"]["unresolved_transceiver_sites"], 0)
        manifest = read_json(output / "serial_wrapper_manifest.json")
        self.assertNotIn(
            "transceiver_site",
            manifest["phy_contract"]["required_provider_fields"],
        )
        self.assertIn(
            "reference_clock_package_binding",
            manifest["phy_contract"]["required_provider_fields"],
        )
        self.assertEqual(
            {
                quad["common_site"]
                for fpga in manifest["fpgas"]
                for quad in fpga["transceiver_quads"]
            },
            {"GTYE4_COMMON_X0Y5", "GTYE4_COMMON_X0Y9"},
        )
        gt_tcl = (output / "mps4_1.gt_sites.tcl").read_text()
        self.assertIn(
            "set_property LOC GTYE4_CHANNEL_X0Y20 "
            "[get_cells {serial_wrapper/site_0_phy/gty_channel}]",
            gt_tcl,
        )
        self.assertFalse((output / "mps4_1.board_services.xdc").exists())

    def test_v2_provider_instantiates_one_shared_common_per_quad(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-v2.json"
        overlay_path = root / "overlay-v2.json"
        gt_site_map_path = root / "gt-site-map-v2.json"
        output = root / "phase6c-v2"
        overlay = copy.deepcopy(self.overlay)
        mapped_sites = {"mps4_1": "GTYE4_CHANNEL_X0Y20", "mps4_2": "GTYE4_CHANNEL_X0Y36"}
        for record in overlay["transceiver_sites"]:
            record["site"] = mapped_sites[record["fpga"]]
        write_json(binding_path, self.binding)
        write_json(overlay_path, overlay)
        write_json(gt_site_map_path, self._gt_site_map())
        report = run_phase6c(
            self.platform_path,
            binding_path,
            output,
            board_overlay_path=overlay_path,
            phy_provider_path=self._write_phy_provider(
                "editable_source_hardware", "emuflow.serial-phy-provider/v2"
            ),
            gt_site_map_path=gt_site_map_path,
        )
        self.assertEqual(
            report["hardware_release_status"],
            "blocked_on_external_phy_provider",
        )
        manifest = read_json(output / "serial_wrapper_manifest.json")
        self.assertEqual(
            manifest["phy_contract"]["required_provider_fields"],
            [
                "fabric_clock_phase_alignment",
                "synchronous_reset_release",
                "global_ready_consensus",
            ],
        )
        self.assertEqual(
            manifest["phy_contract"]["module"],
            "emuflow_external_serial_phy_quad",
        )
        self.assertEqual(
            manifest["metrics"]["provider_source_bound_phy_modules"], 2
        )
        self.assertEqual(manifest["metrics"]["active_phy_modules"], 2)
        self.assertEqual(manifest["metrics"]["unresolved_phy_modules"], 0)
        wrapper = (output / "mps4_1.serial_wrapper.sv").read_text()
        self.assertEqual(
            wrapper.count("emuflow_external_serial_phy_quad #("), 1
        )
        self.assertIn(".ACTIVE_CHANNEL_MASK(4'b0001)", wrapper)
        self.assertNotIn("emuflow_external_serial_phy_lane #(", wrapper)
        tcl = (output / "mps4_1.gt_sites.tcl").read_text()
        self.assertNotIn("if {", tcl)
        self.assertIn(
            "set_property LOC GTYE4_COMMON_X0Y5 "
            "[get_cells {serial_wrapper/quad_0_phy/gty_common}]",
            tcl,
        )
        self.assertIn(
            "set_property LOC GTYE4_CHANNEL_X0Y20 "
            "[get_cells {serial_wrapper/quad_0_phy/channel_0.gty_channel}]",
            tcl,
        )

    def test_v3_embeds_open_pcs_and_runtime_sync_in_wrapper(self) -> None:
        root = Path(self.temporary_directory.name)
        binding_path = root / "binding-v3.json"
        gt_site_map_path = root / "gt-site-map-v3.json"
        topology_path = root / "runtime-sync-v3.json"
        output = root / "phase6c-v3"
        v3_binding = copy.deepcopy(self.binding)
        lane_one = copy.deepcopy(v3_binding["entries"][0])
        source_lane = self.platform.links[0].endpoint_binding("mps4_1").lanes[1]
        sink_lane = self.platform.links[0].endpoint_binding("mps4_2").lanes[1]
        lane_one.update(
            {
                "id": "mps4_b2b_1:mps4_1-to-mps4_2:gty-1",
                "physical_lane": 1,
                "logical_lanes": [64, 65],
                "logical_bindings": ["logical-64", "logical-65"],
                "source_ports": {
                    "p": "gty_txp_mps4_b2b_1_mps4_2_lane1",
                    "n": "gty_txn_mps4_b2b_1_mps4_2_lane1",
                },
                "sink_ports": {
                    "p": "gty_rxp_mps4_b2b_1_mps4_1_lane1",
                    "n": "gty_rxn_mps4_b2b_1_mps4_1_lane1",
                },
                "source_package_pins": {
                    "p": source_lane.tx_package_pin_p,
                    "n": source_lane.tx_package_pin_n,
                },
                "sink_package_pins": {
                    "p": sink_lane.rx_package_pin_p,
                    "n": sink_lane.rx_package_pin_n,
                },
            }
        )
        v3_binding["entries"].append(lane_one)
        write_json(binding_path, v3_binding)
        write_json(gt_site_map_path, self._gt_site_map())
        transport_paths = {}
        for fpga, transport in self.transports.items():
            path = root / f"{fpga}.v3.transport.json"
            write_json(path, transport)
            transport_paths[fpga] = path
        runtime_provider_path = (
            Path(__file__).resolve().parents[1]
            / "providers/runtime_sync_tree/provider.json"
        )
        runtime_provider = validate_runtime_sync_provider(
            read_json(runtime_provider_path), runtime_provider_path
        )["normalized"]
        topology = build_runtime_sync_topology(self.platform, runtime_provider)
        write_json(topology_path, topology)
        report = run_phase6c(
            self.platform_path,
            binding_path,
            output,
            transport_paths=transport_paths,
            phy_provider_path=self._write_v3_phy_provider(),
            gt_site_map_path=gt_site_map_path,
            runtime_sync_topology_path=topology_path,
            runtime_sync_provider_path=runtime_provider_path,
        )
        self.assertEqual(
            report["hardware_release_status"],
            "blocked_on_board_latency_and_clock_proof",
        )
        self.assertIn("open_pcs_rtl", report["artifacts"])
        self.assertGreater(len(report["artifacts"]["open_pcs_rtl"]), 10)
        manifest = read_json(output / "serial_wrapper_manifest.json")
        self.assertEqual(
            manifest["phy_contract"]["module"],
            "emuflow_external_gty_serdes_quad",
        )
        self.assertIn(
            "runtime_sync_control_transport_latency",
            manifest["phy_contract"]["required_provider_fields"],
        )
        root_wrapper = (output / "mps4_1.serial_wrapper.sv").read_text()
        root_shell = (
            output / "mps4_1.serial_integration_shell.sv"
        ).read_text()
        self.assertIn("emuflow_external_gty_serdes_quad", root_wrapper)
        self.assertIn("emuflow_runtime_sync_pcs_edge", root_wrapper)
        self.assertIn("emuflow_data_pcs_edge", root_wrapper)
        self.assertIn("emuflow_runtime_sync_tree_node", root_wrapper)
        self.assertNotIn("emuflow_runtime_sync_tree_node", root_shell)
        self.assertIn("assign links_ready = local_links_ready", root_shell)
        leaf = next(
            item for item in manifest["fpgas"] if item["fpga"] == "mps4_3"
        )
        self.assertEqual(len(leaf["runtime_sync_edges"]), 1)
        self.assertEqual(leaf["runtime_sync_edges"][0]["role"], "child")
        self.assertGreater(leaf["active_transceiver_sites"], 0)
        iverilog = shutil.which("iverilog")
        if iverilog is not None:
            source_files = [
                root / "provider-simulation-v3.sv",
                *(
                    output / relative
                    for relative in report["artifacts"]["runtime_sync_rtl"]
                ),
                *(
                    output / relative
                    for relative in report["artifacts"]["open_pcs_rtl"]
                ),
                output / "mps4_1.serial_wrapper.sv",
            ]
            completed = subprocess.run(
                [
                    iverilog,
                    "-g2012",
                    "-s",
                    "emuflow_serial_wrapper_mps4_1",
                    "-o",
                    str(root / "v3-wrapper.vvp"),
                    *(str(path) for path in source_files),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )


if __name__ == "__main__":
    unittest.main()
