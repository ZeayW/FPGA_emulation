import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from emuflow.board_arm_mps4 import materialize_arm_mps4_boarddb
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.platform import Platform
from emuflow.serial_phy_provider import (
    SERIAL_PHY_PROVIDER_SCHEMA,
    SERIAL_PHY_PROVIDER_V2_SCHEMA,
    SERIAL_PHY_PROVIDER_V3_SCHEMA,
    validate_serial_phy_provider,
    validate_serial_phy_provider_file,
)


PROVIDER_SOURCE = """module emuflow_external_serial_clock_reset #(
  parameter integer BOARD_RESET_ACTIVE_LOW = 1
) (input wire refclk_p, input wire refclk_n, input wire board_reset,
   output wire phy_refclk, output wire phy_reset, output wire ready);
  assign phy_refclk = refclk_p;
  assign phy_reset = BOARD_RESET_ACTIVE_LOW ? ~board_reset : board_reset;
  assign ready = 1'b1;
endmodule

module emuflow_external_serial_phy_lane #(
  parameter integer PAYLOAD_WIDTH = 64
) (input wire user_clk, input wire reset, input wire phy_refclk,
   input wire phy_reset, input wire [PAYLOAD_WIDTH-1:0] tx_data,
   output wire [PAYLOAD_WIDTH-1:0] rx_data, output wire txp,
   output wire txn, input wire rxp, input wire rxn, output wire ready);
  assign rx_data = {PAYLOAD_WIDTH{1'b0}};
  assign txp = 1'b0;
  assign txn = 1'b1;
  assign ready = 1'b1;
endmodule
"""

QUAD_PROVIDER_SOURCE = """module emuflow_external_serial_clock_reset #(
  parameter integer BOARD_RESET_ACTIVE_LOW = 1
) (input wire refclk_p, input wire refclk_n, input wire board_reset,
   output wire phy_refclk, output wire phy_reset, output wire ready);
  IBUFDS_GTE4 refclk_buffer ();
  assign phy_refclk = refclk_p;
  assign phy_reset = BOARD_RESET_ACTIVE_LOW ? ~board_reset : board_reset;
  assign ready = 1'b1;
endmodule

module emuflow_external_serial_phy_quad #(
  parameter integer PAYLOAD_WIDTH = 64,
  parameter [3:0] ACTIVE_CHANNEL_MASK = 4'b0000
) (input wire user_clk, input wire reset, input wire phy_refclk,
   input wire phy_reset, input wire [4*PAYLOAD_WIDTH-1:0] tx_data,
   output wire [4*PAYLOAD_WIDTH-1:0] rx_data,
   output wire [3:0] txp, output wire [3:0] txn,
   input wire [3:0] rxp, input wire [3:0] rxn,
   output wire [3:0] lane_ready, output wire common_ready);
  GTYE4_COMMON gty_common ();
  GTYE4_CHANNEL gty_channel ();
  assign rx_data = {4*PAYLOAD_WIDTH{1'b0}};
  assign txp = 4'b0000; assign txn = 4'b1111;
  assign lane_ready = ACTIVE_CHANNEL_MASK;
  assign common_ready = 1'b1;
endmodule
"""

SERDES_PROVIDER_SOURCE = """module emuflow_external_serial_clock_reset #(
  parameter integer BOARD_RESET_ACTIVE_LOW = 1
) (input wire refclk_p, input wire refclk_n, input wire board_reset,
   output wire phy_refclk, output wire phy_reset, output wire ready);
  IBUFDS_GTE4 refclk_buffer ();
  assign phy_refclk = refclk_p;
  assign phy_reset = BOARD_RESET_ACTIVE_LOW ? ~board_reset : board_reset;
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
  GTYE4_COMMON gty_common ();
  GTYE4_CHANNEL gty_channel ();
  assign serdes_rx_data = 256'b0; assign serdes_rx_hdr = 8'b0;
  assign tx_usrclk = 4'b0; assign rx_usrclk = 4'b0;
  assign txp = 4'b0; assign txn = 4'b1;
  assign lane_ready = ACTIVE_CHANNEL_MASK; assign common_ready = 1'b1;
endmodule
"""


class SerialPhyProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.root = root
        self.platform_path = root / "platform.json"
        materialize_arm_mps4_boarddb(
            self.platform_path,
            name="provider_fixture",
            fabric_clock_mhz=50.0,
            payload_bits_per_lane_per_cycle=64,
            latency_cycles=4,
        )
        self.platform = Platform.load(self.platform_path)
        self.source_path = root / "provider.sv"
        self.source_path.write_text(PROVIDER_SOURCE, encoding="utf-8")
        digest = hashlib.sha256(PROVIDER_SOURCE.encode("utf-8")).hexdigest()
        self.manifest_path = root / "provider.json"
        self.manifest = {
            "schema": SERIAL_PHY_PROVIDER_SCHEMA,
            "id": "structural_provider_fixture",
            "qualification": "simulation_only",
            "supported_parts": ["xcvu13p-fhga2104-1-e"],
            "modules": {
                "clock_reset": "emuflow_external_serial_clock_reset",
                "lane": "emuflow_external_serial_phy_lane",
            },
            "implementation": {"kind": "behavioral"},
            "source_root": ".",
            "sources": [
                {
                    "path": "provider.sv",
                    "language": "systemverilog",
                    "role": "structural_test_fixture",
                    "sha256": digest,
                }
            ],
            "protocol": {
                "payload_bits_per_lane_per_cycle": 64,
                "user_clock_mhz": 50.0,
                "line_rate_gbps_per_lane": 10.0,
                "encoding": "test_only_no_line_encoding",
                "link_training": "test_only_always_ready",
                "reset_sequence": "test_only_combinational_reset",
            },
            "provenance": {
                "license": "Apache-2.0-test-fixture",
                "upstream": "repository unit test",
            },
        }
        write_json(self.manifest_path, self.manifest)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validates_editable_source_and_platform_compatibility(self) -> None:
        result = validate_serial_phy_provider(
            self.manifest, self.manifest_path, self.platform
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["editable_sources"], 1)
        self.assertEqual(result["compatibility"]["serial_links"], 3)
        self.assertEqual(result["compatibility"]["status"], "compatible")
        normalized = self.root / "normalized.json"
        report = validate_serial_phy_provider_file(
            self.manifest_path, self.platform_path, normalized
        )
        self.assertEqual(report["provider"], "structural_provider_fixture")
        self.assertEqual(read_json(normalized)["sources"][0]["bytes"], len(
            PROVIDER_SOURCE.encode("utf-8")
        ))

    def test_rejects_tampered_or_opaque_source(self) -> None:
        tampered = copy.deepcopy(self.manifest)
        tampered["sources"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "SHA-256 mismatch"):
            validate_serial_phy_provider(tampered, self.manifest_path)
        opaque_path = self.root / "provider.dcp"
        opaque_path.write_bytes(b"opaque checkpoint")
        opaque = copy.deepcopy(self.manifest)
        opaque["sources"][0] = {
            "path": "provider.dcp",
            "language": "systemverilog",
            "role": "forbidden_binary",
            "sha256": hashlib.sha256(b"opaque checkpoint").hexdigest(),
        }
        with self.assertRaisesRegex(ValidationError, "opaque"):
            validate_serial_phy_provider(opaque, self.manifest_path)

    def test_rejects_missing_module_or_unsupported_part(self) -> None:
        missing = copy.deepcopy(self.manifest)
        self.source_path.write_text("module unrelated; endmodule\n", encoding="utf-8")
        missing["sources"][0]["sha256"] = hashlib.sha256(
            self.source_path.read_bytes()
        ).hexdigest()
        with self.assertRaisesRegex(ValidationError, "does not define"):
            validate_serial_phy_provider(missing, self.manifest_path)
        self.source_path.write_text(PROVIDER_SOURCE, encoding="utf-8")
        unsupported = copy.deepcopy(self.manifest)
        unsupported["supported_parts"] = ["some-other-part"]
        with self.assertRaisesRegex(ValidationError, "does not support"):
            validate_serial_phy_provider(
                unsupported, self.manifest_path, self.platform
            )

    def test_rejects_protocol_outside_board_contract(self) -> None:
        incompatible = copy.deepcopy(self.manifest)
        incompatible["protocol"]["user_clock_mhz"] = 62.5
        with self.assertRaisesRegex(ValidationError, "user_clock"):
            validate_serial_phy_provider(
                incompatible, self.manifest_path, self.platform
            )
        incompatible = copy.deepcopy(self.manifest)
        incompatible["protocol"]["line_rate_gbps_per_lane"] = 25.1
        with self.assertRaisesRegex(ValidationError, "board_ceiling"):
            validate_serial_phy_provider(
                incompatible, self.manifest_path, self.platform
            )

    def test_hardware_qualification_rejects_black_box_source(self) -> None:
        source_text = """(* black_box *) module emuflow_external_serial_clock_reset; endmodule
(* black_box *) module emuflow_external_serial_phy_lane; endmodule
"""
        self.source_path.write_text(source_text, encoding="utf-8")
        hardware = copy.deepcopy(self.manifest)
        hardware["qualification"] = "editable_source_hardware"
        hardware["implementation"] = {
            "kind": "amd_ultrascale_plus_gty",
            "channel_primitive": "GTYE4_CHANNEL",
            "reference_clock_primitive": "IBUFDS_GTE4",
            "channel_instance": "gty_channel",
            "reference_clock_instance": "refclk_buffer",
        }
        hardware["sources"][0]["sha256"] = hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValidationError, "black-box"):
            validate_serial_phy_provider(hardware, self.manifest_path)

    def test_validates_quad_aware_v2_provider_contract(self) -> None:
        source = self.root / "quad-provider.sv"
        source.write_text(QUAD_PROVIDER_SOURCE, encoding="utf-8")
        manifest = copy.deepcopy(self.manifest)
        manifest.update(
            {
                "schema": SERIAL_PHY_PROVIDER_V2_SCHEMA,
                "id": "quad_provider_fixture",
                "modules": {
                    "clock_reset": "emuflow_external_serial_clock_reset",
                    "quad": "emuflow_external_serial_phy_quad",
                },
                "source_root": ".",
                "sources": [
                    {
                        "path": source.name,
                        "language": "systemverilog",
                        "role": "quad_contract_fixture",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
        result = validate_serial_phy_provider(
            manifest, self.manifest_path, self.platform
        )
        self.assertEqual(result["normalized"]["schema"], SERIAL_PHY_PROVIDER_V2_SCHEMA)
        self.assertEqual(
            result["normalized"]["modules"]["quad"],
            "emuflow_external_serial_phy_quad",
        )

    def test_validates_open_pcs_serdes_v3_provider_contract(self) -> None:
        source = self.root / "serdes-provider.sv"
        source.write_text(SERDES_PROVIDER_SOURCE, encoding="utf-8")
        manifest = copy.deepcopy(self.manifest)
        manifest.update(
            {
                "schema": SERIAL_PHY_PROVIDER_V3_SCHEMA,
                "id": "serdes_provider_fixture",
                "qualification": "editable_source_hardware",
                "modules": {
                    "clock_reset": "emuflow_external_serial_clock_reset",
                    "serdes_quad": "emuflow_external_gty_serdes_quad",
                },
                "implementation": {
                    "kind": "amd_ultrascale_plus_gty",
                    "channel_primitive": "GTYE4_CHANNEL",
                    "common_primitive": "GTYE4_COMMON",
                    "reference_clock_primitive": "IBUFDS_GTE4",
                    "channel_instance_template": "channel_{channel}/gty_channel",
                    "common_instance": "gty_common",
                    "reference_clock_instance": "refclk_buffer",
                },
                "sources": [
                    {
                        "path": source.name,
                        "language": "systemverilog",
                        "role": "parallel_66b_serdes_fixture",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
        manifest["protocol"].update(
            {
                "encoding": "64b66b",
                "line_rate_gbps_per_lane": 10.3125,
                "pcs_data_width": 64,
                "pcs_header_width": 2,
                "pcs_clock_mhz": 156.25,
                "pcs_implementation": "emuflow-in-tree-corundum-10gbase-r",
            }
        )
        result = validate_serial_phy_provider(
            manifest, self.manifest_path, self.platform
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["normalized"]["modules"]["serdes_quad"],
            "emuflow_external_gty_serdes_quad",
        )
        self.assertEqual(result["normalized"]["protocol"]["pcs_clock_mhz"], 156.25)
        wrong_rate = copy.deepcopy(manifest)
        wrong_rate["protocol"]["line_rate_gbps_per_lane"] = 10.0
        with self.assertRaisesRegex(ValidationError, "pcs_clock_mhz"):
            validate_serial_phy_provider(wrong_rate, self.manifest_path)

        overloaded = copy.deepcopy(manifest)
        overloaded["protocol"]["user_clock_mhz"] = 100.0
        # 6.4 Gbps is below the 10.3125-Gbps line rate but exceeds the
        # three-cycle record envelope's sustainable payload rate.
        with self.assertRaisesRegex(ValidationError, "three-cycle PCS"):
            validate_serial_phy_provider(overloaded, self.manifest_path)
        boundary = copy.deepcopy(manifest)
        boundary["protocol"]["user_clock_mhz"] = 156.25 / 3
        self.assertEqual(validate_serial_phy_provider(
            boundary, self.manifest_path)["status"], "pass")
        wrong_width = copy.deepcopy(manifest)
        wrong_width["protocol"]["payload_bits_per_lane_per_cycle"] = 128
        with self.assertRaisesRegex(ValidationError, "record payload"):
            validate_serial_phy_provider(wrong_width, self.manifest_path)

    def test_v2_hardware_requires_common_and_channel_hierarchy(self) -> None:
        source = self.root / "quad-hardware.sv"
        source.write_text(QUAD_PROVIDER_SOURCE, encoding="utf-8")
        manifest = copy.deepcopy(self.manifest)
        manifest.update(
            {
                "schema": SERIAL_PHY_PROVIDER_V2_SCHEMA,
                "qualification": "editable_source_hardware",
                "modules": {
                    "clock_reset": "emuflow_external_serial_clock_reset",
                    "quad": "emuflow_external_serial_phy_quad",
                },
                "implementation": {
                    "kind": "amd_ultrascale_plus_gty",
                    "channel_primitive": "GTYE4_CHANNEL",
                    "common_primitive": "GTYE4_COMMON",
                    "reference_clock_primitive": "IBUFDS_GTE4",
                    "channel_instance_template": "channel_{channel}.gty_channel",
                    "common_instance": "gty_common",
                    "reference_clock_instance": "refclk_buffer",
                },
                "source_root": ".",
                "sources": [
                    {
                        "path": source.name,
                        "language": "systemverilog",
                        "role": "quad_hardware_fixture",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
        result = validate_serial_phy_provider(manifest, self.manifest_path)
        self.assertEqual(
            result["normalized"]["implementation"]["common_primitive"],
            "GTYE4_COMMON",
        )
        malformed = copy.deepcopy(manifest)
        malformed["implementation"]["channel_instance_template"] = "channel/gty"
        with self.assertRaisesRegex(ValidationError, "placeholder"):
            validate_serial_phy_provider(malformed, self.manifest_path)

    def test_validates_vendor_generated_v3_provider_without_calling_it_open(self) -> None:
        source_text = SERDES_PROVIDER_SOURCE.replace(
            "  GTYE4_COMMON gty_common ();\n  GTYE4_CHANNEL gty_channel ();",
            "  emuflow_gty_10g_full full_ip ();\n"
            "  emuflow_gty_10g_channel channel_ip ();",
        )
        source = self.root / "vendor-adapter.sv"
        source.write_text(source_text, encoding="utf-8")
        xci_records = []
        for name in ("emuflow_gty_10g_full", "emuflow_gty_10g_channel"):
            xci = self.root / f"{name}.xci"
            xci.write_text(f"fixture {name}\n", encoding="utf-8")
            xci_records.append(
                {"path": xci.name, "sha256": hashlib.sha256(xci.read_bytes()).hexdigest()}
            )
        manifest = copy.deepcopy(self.manifest)
        manifest.update(
            {
                "schema": SERIAL_PHY_PROVIDER_V3_SCHEMA,
                "id": "vendor_serdes_provider_fixture",
                "qualification": "vendor_generated_hardware",
                "modules": {
                    "clock_reset": "emuflow_external_serial_clock_reset",
                    "serdes_quad": "emuflow_external_gty_serdes_quad",
                },
                "implementation": {
                    "kind": "amd_ultrascale_plus_gty",
                    "channel_primitive": "GTYE4_CHANNEL",
                    "common_primitive": "GTYE4_COMMON",
                    "reference_clock_primitive": "IBUFDS_GTE4",
                    "channel_instance_template": "channel_gen[{channel}].adapter",
                    "common_instance": "channel_gen",
                    "reference_clock_instance": "refclk_buffer",
                    "hierarchy_resolution": "descendant_primitive_sorted",
                },
                "sources": [
                    {
                        "path": source.name,
                        "language": "systemverilog",
                        "role": "vendor_adapter_fixture",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
                "vendor_products": {
                    "generator": "vivado_gtwizard_ultrascale",
                    "modules": [
                        "emuflow_gty_10g_full",
                        "emuflow_gty_10g_channel",
                    ],
                    "xci": xci_records,
                },
            }
        )
        manifest["protocol"].update(
            {
                "encoding": "64b66b",
                "line_rate_gbps_per_lane": 10.3125,
                "pcs_data_width": 64,
                "pcs_header_width": 2,
                "pcs_clock_mhz": 156.25,
                "pcs_implementation": "emuflow-in-tree-corundum-10gbase-r",
            }
        )
        result = validate_serial_phy_provider(
            manifest, self.manifest_path, self.platform
        )
        self.assertEqual(result["qualification"], "vendor_generated_hardware")
        self.assertFalse(
            result["normalized"]["vendor_products"][
                "counts_as_open_flow_implementation"
            ]
        )
        self.assertEqual(len(result["normalized"]["vendor_products"]["xci"]), 2)


if __name__ == "__main__":
    unittest.main()
