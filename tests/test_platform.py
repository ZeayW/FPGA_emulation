import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.routing import build_directed_graph, normalize_route_constraints


ROOT = Path(__file__).resolve().parents[1]


class PlatformTest(unittest.TestCase):
    def test_reference_platform(self) -> None:
        platform = Platform.load(
            ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
        )
        self.assertEqual(platform.name, "virtual_xcvu3p_2fpga_p2p")
        self.assertEqual(len(platform.fpgas), 2)
        self.assertEqual(len(platform.links), 1)
        self.assertEqual(platform.fpgas[0].effective_capacity["lut"], 295560)
        self.assertEqual(platform.fpgas[0].effective_capacity["bram"], 540)
        self.assertEqual(platform.fpgas[0].effective_capacity["dsp"], 1710)
        self.assertAlmostEqual(
            platform.links[0].raw_bits_per_second_per_direction,
            8_000_000_000.0,
        )

    def test_default_academic_platform_has_no_vendor_part(self) -> None:
        platform = Platform.load(
            ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"
        )
        self.assertEqual(platform.name, "academic_vtr_2fpga_p2p")
        self.assertEqual(len(platform.fpgas), 2)
        self.assertTrue(
            all(fpga.part.startswith("vtr-") for fpga in platform.fpgas)
        )

    def test_eight_fpga_scale_platform(self) -> None:
        platform = Platform.load(
            ROOT / "platforms/virtual/xcvu3p_8fpga_mesh.json"
        )
        self.assertEqual(platform.name, "virtual_xcvu3p_8fpga_mesh")
        self.assertEqual(len(platform.fpgas), 8)
        self.assertEqual(len(platform.links), 10)
        self.assertEqual(
            sum(fpga.effective_capacity["lut"] for fpga in platform.fpgas),
            2_364_480,
        )

    def test_four_fpga_nvdla_platform(self) -> None:
        platform = Platform.load(
            ROOT / "platforms/virtual/xcvu3p_4fpga_mesh.json"
        )
        self.assertEqual(platform.name, "virtual_xcvu3p_4fpga_mesh")
        self.assertEqual(len(platform.fpgas), 4)
        self.assertEqual(len(platform.links), 4)
        self.assertEqual(
            sum(fpga.effective_capacity["lut"] for fpga in platform.fpgas),
            1_576_320,
        )

    def test_four_fpga_vu9p_platform_with_headroom(self) -> None:
        platform = Platform.load(
            ROOT / "platforms/virtual/xcvu9p_4fpga_mesh.json"
        )
        self.assertEqual(platform.name, "virtual_xcvu9p_4fpga_mesh")
        self.assertEqual(len(platform.fpgas), 4)
        self.assertEqual(len(platform.links), 4)
        self.assertEqual(
            sum(fpga.effective_capacity["lut"] for fpga in platform.fpgas),
            3_546_720,
        )

    def test_four_fpga_rapidwright_research_platform(self) -> None:
        platform = Platform.load(
            ROOT / "platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json"
        )
        self.assertEqual(platform.name, "rapidwright_xcvu19p_4fpga_mesh")
        self.assertEqual(len(platform.fpgas), 4)
        self.assertEqual(len(platform.links), 4)
        self.assertTrue(
            all(fpga.part == "xcvu19p-fsva3824-2-e" for fpga in platform.fpgas)
        )
        self.assertEqual(
            sum(fpga.effective_capacity["lut"] for fpga in platform.fpgas),
            12_257_280,
        )

    def test_unknown_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown FPGA IDs"):
            Platform.from_dict(
                {
                    "schema": "emuflow.boarddb/v1",
                    "platform": {"name": "bad", "kind": "virtual"},
                    "fpgas": [
                        {
                            "id": "fpga0",
                            "part": "xcvu3p",
                            "utilization_limit": 0.75,
                            "capacity": {"lut": 10},
                        }
                    ],
                    "links": [
                        {
                            "id": "bad_link",
                            "endpoints": ["fpga0", "fpga1"],
                            "direction": "full_duplex",
                            "mode": "abstract",
                            "data_lanes_per_direction": 1,
                            "fabric_clock_mhz": 1,
                            "latency_cycles": 0,
                        }
                    ],
                }
            )

    def test_shared_bidirectional_capacity_is_a_default_route_domain(self) -> None:
        platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {"name": "shared", "kind": "virtual"},
                "fpgas": [
                    {
                        "id": fpga_id,
                        "part": "academic",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 10},
                    }
                    for fpga_id in ("F0", "F1")
                ],
                "links": [
                    {
                        "id": "shared_link",
                        "endpoints": ["F0", "F1"],
                        "direction": "full_duplex",
                        "capacity_sharing": "shared_bidirectional",
                        "mode": "abstract",
                        "data_lanes_per_direction": 1,
                        "fabric_clock_mhz": 50,
                        "latency_cycles": 2,
                    }
                ],
            }
        )
        self.assertEqual(
            platform.links[0].capacity_sharing, "shared_bidirectional"
        )
        constraints = normalize_route_constraints(None, platform)
        self.assertEqual(
            constraints["shared_capacity_links"], ["shared_link"]
        )
        _, arcs, capacity_records = build_directed_graph(platform, constraints)
        self.assertEqual(len(arcs), 2)
        self.assertEqual(
            {arc["capacity_key"] for arc in arcs.values()},
            {"shared_link:shared"},
        )
        self.assertEqual(set(capacity_records), {"shared_link:shared"})

    def test_shared_bidirectional_capacity_requires_full_duplex(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires full_duplex"):
            Platform.from_dict(
                {
                    "schema": "emuflow.boarddb/v1",
                    "platform": {"name": "bad", "kind": "virtual"},
                    "fpgas": [
                        {
                            "id": fpga_id,
                            "part": "academic",
                            "utilization_limit": 1.0,
                            "capacity": {"lut": 10},
                        }
                        for fpga_id in ("F0", "F1")
                    ],
                    "links": [
                        {
                            "id": "bad_shared_link",
                            "endpoints": ["F0", "F1"],
                            "direction": "half_duplex",
                            "capacity_sharing": "shared_bidirectional",
                            "mode": "abstract",
                            "data_lanes_per_direction": 1,
                            "fabric_clock_mhz": 50,
                            "latency_cycles": 2,
                        }
                    ],
                }
            )

    def test_serial_link_separates_physical_lanes_from_transport_width(self) -> None:
        platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {"name": "serial", "kind": "hardware"},
                "fpgas": [
                    {
                        "id": fpga_id,
                        "part": "xcvu13p-1fhga2104e",
                        "utilization_limit": 0.75,
                        "capacity": {"lut": 1_728_000},
                    }
                    for fpga_id in ("F0", "F1")
                ],
                "links": [
                    {
                        "id": "gty",
                        "endpoints": ["F0", "F1"],
                        "direction": "full_duplex",
                        "mode": "serial",
                        "data_lanes_per_direction": 12,
                        "payload_bits_per_lane_per_cycle": 64,
                        "fabric_clock_mhz": 390.625,
                        "max_line_rate_gbps_per_lane": 25.0,
                        "latency_cycles": 4,
                    }
                ],
            }
        )
        link = platform.links[0]
        self.assertEqual(link.data_lanes_per_direction, 12)
        self.assertEqual(link.transport_bits_per_cycle_per_direction, 768)
        self.assertEqual(link.raw_bits_per_second_per_direction, 300e9)
        constraints = normalize_route_constraints(None, platform)
        _, _, capacities = build_directed_graph(platform, constraints)
        self.assertEqual(
            capacities["gty:F0->F1"]["capacity_bits"], 768 * 32
        )

    def test_serial_user_rate_cannot_exceed_line_rate(self) -> None:
        with self.assertRaisesRegex(ValidationError, "exceeds maximum"):
            Platform.from_dict(
                {
                    "schema": "emuflow.boarddb/v1",
                    "platform": {"name": "bad", "kind": "hardware"},
                    "fpgas": [
                        {
                            "id": fpga_id,
                            "part": "xcvu13p",
                            "utilization_limit": 1.0,
                            "capacity": {"lut": 1},
                        }
                        for fpga_id in ("F0", "F1")
                    ],
                    "links": [
                        {
                            "id": "gty",
                            "endpoints": ["F0", "F1"],
                            "direction": "full_duplex",
                            "mode": "serial",
                            "data_lanes_per_direction": 1,
                            "payload_bits_per_lane_per_cycle": 64,
                            "fabric_clock_mhz": 500,
                            "max_line_rate_gbps_per_lane": 25,
                            "latency_cycles": 1,
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
