import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.board_arm_mps4 import materialize_arm_mps4_boarddb
from emuflow.netlist import _build_virtual_anchors
from emuflow.physical_pins import (
    LEGACY_PACKAGE_PIN_PROVIDERS,
    PACKAGE_PIN_PROVIDER,
    SERIAL_TRANSCEIVER_PROVIDER,
    binding_to_xdc,
    build_package_pin_binding,
    build_serial_transceiver_binding,
    run_phase6b,
    validate_hardware_bsp,
    validate_package_pin_binding,
    validate_serial_transceiver_binding,
)
from emuflow.io import read_json, write_json
from tests.pin_plan_fixture import make_pin_plan_fixture
from emuflow.platform import Platform
from tests.schedule_store_fixture import write_tdm_schedule_storage


ROOT = Path(__file__).resolve().parents[1]


class PhysicalPinBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(cls.temporary_directory.name)
        cls.bsp_solver = root / "emuflow_bsp_pin_solver"
        for source, output in (
            ("bsp_pin_solver.cpp", cls.bsp_solver),
        ):
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-O2",
                    "-pthread",
                    str(ROOT / "src/native" / source),
                    "-o",
                    str(output),
                ],
                check=True,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "physical_pin_fixture",
                    "kind": "virtual",
                },
                "fpgas": [
                    {
                        "id": fpga,
                        "part": "xcvu9p-flga2104-2L-e",
                        "utilization_limit": 0.75,
                        "capacity": {"lut": 100, "ff": 100},
                    }
                    for fpga in ("fpga0", "fpga1")
                ],
                "links": [
                    {
                        "id": "link0",
                        "endpoints": ["fpga0", "fpga1"],
                        "direction": "full_duplex",
                        "mode": "source_synchronous",
                        "data_lanes_per_direction": 2,
                        "fabric_clock_mhz": 250,
                        "latency_cycles": 2,
                    }
                ],
            }
        )
        self.schedule = {
            "design": "physical_pin_fixture",
            "platform": self.platform.name,
            "entries": [
                {
                    "id": f"entry{index}",
                    "link": "link0",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tdm_ratio": 2,
                    "slot": index % 2,
                    "lane": index // 2,
                }
                for index in range(4)
            ],
        }
        self.positions = {
            "schema": "emuflow.signal-position-hints/v1",
            "design": self.schedule["design"],
            "platform": self.platform.name,
            "provider": "openparf-lookahead-centroid-v1",
            "region_count": 3,
            "metrics": {
                "signals": 4,
                "endpoint_centroid_fallbacks": 0,
            },
            "entries": [
                {
                    "schedule_entry": f"entry{index}",
                    "source_y": 0.1 if index < 2 else 0.9,
                    "sink_y": 0.15 if index < 2 else 0.85,
                    "source_region": 0 if index < 2 else 2,
                    "sink_region": 0 if index < 2 else 2,
                    "source_fallback": False,
                    "sink_fallback": False,
                }
                for index in range(4)
            ],
        }
        self.plan = make_pin_plan_fixture(
            self.schedule,
            self.platform,
            self.positions,
        )
        self.anchors = {
            fpga: {
                "schema": "emuflow.virtual-io-anchors/v1",
                "platform": self.platform.name,
                "fpga": fpga,
                "part": "xcvu9p-flga2104-2L-e",
                "anchors": [],
                "required_hardware_binding_fields": [
                    "package_pin",
                    "bank",
                    "iostandard",
                ],
            }
            for fpga in ("fpga0", "fpga1")
        }
        by_group = {}
        for item in self.plan["entries"]:
            by_group.setdefault(item["group"], item["physical_lane"])
        for lane in sorted(by_group.values()):
            self.anchors["fpga0"]["anchors"].append(
                {
                    "id": f"link0:fpga0:tx:{lane}",
                    "link": "link0",
                    "peer": "fpga1",
                    "direction": "tx",
                    "logical_lane": lane,
                    "binding_status": "unbound",
                }
            )
            self.anchors["fpga1"]["anchors"].append(
                {
                    "id": f"link0:fpga1:rx:{lane}",
                    "link": "link0",
                    "peer": "fpga0",
                    "direction": "rx",
                    "logical_lane": lane,
                    "binding_status": "unbound",
                }
            )
        self.bsp = self._bsp()

    def _bsp(self, iostandard: str = "LVCMOS18"):
        voltage = {
            "LVCMOS12": 1.2,
            "LVCMOS18": 1.8,
        }[iostandard]
        fpgas = []
        for fpga, direction in (("fpga0", "tx"), ("fpga1", "rx")):
            pins = []
            for index, region_y in enumerate((0.9, 0.1)):
                pins.append(
                    {
                        "id": f"{fpga}:pin{index}",
                        "fpga": fpga,
                        "package_pin": f"{fpga.upper()}_P{index}",
                        "bank": "BANK0",
                        "connector": "J0",
                        "connector_pin": index,
                        "directions": [direction],
                        "iostandards": [iostandard],
                        "region_y": region_y,
                        "clock_capable": False,
                        "reserved": False,
                    }
                )
            fpgas.append(
                {
                    "id": fpga,
                    "part": "xcvu9p-flga2104-2L-e",
                    "banks": [
                        {
                            "id": "BANK0",
                            "voltage": voltage,
                            "iostandards": [iostandard],
                            "max_pins": 2,
                        }
                    ],
                    "pins": pins,
                }
            )
        return {
            "schema": "emuflow.hardware-bsp/v1",
            "platform": self.platform.name,
            "board": {
                "name": "synthetic_fixture",
                "revision": "1",
                "qualification": "synthetic_validation",
            },
            "fpgas": fpgas,
            "channels": [
                {
                    "id": f"channel{index}",
                    "link": "link0",
                    "source": "fpga0",
                    "sink": "fpga1",
                    "source_pin": f"fpga0:pin{index}",
                    "sink_pin": f"fpga1:pin{index}",
                    "iostandard": iostandard,
                    "max_frequency_mhz": 300,
                    "skew_ps": index,
                }
                for index in range(2)
            ],
        }

    def _build(self):
        return build_package_pin_binding(
            self.schedule,
            self.platform,
            self.positions,
            self.plan,
            self.anchors,
            self.bsp,
            executable=str(self.bsp_solver),
        )

    def test_exact_binding_is_reproducible_and_placement_aware(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(first, second)
        self.assertEqual(first["provider"], PACKAGE_PIN_PROVIDER)
        self.assertEqual(first["status"], "synthetic_validation")
        validation = validate_package_pin_binding(
            self.schedule,
            self.platform,
            self.positions,
            self.plan,
            self.anchors,
            self.bsp,
            first,
        )
        self.assertEqual(validation["status"], "pass")
        low = min(first["entries"], key=lambda item: item["source_y"])
        high = max(first["entries"], key=lambda item: item["source_y"])
        self.assertEqual(low["channel"], "channel1")
        self.assertEqual(high["channel"], "channel0")
        xdc = binding_to_xdc(first, "fpga0")
        self.assertIn("SYNTHETIC VALIDATION BSP", xdc)
        self.assertIn("get_ports {tx_link0_fpga1[", xdc)

    def test_legacy_binding_provider_remains_readable(self) -> None:
        binding = self._build()
        self.assertNotIn(binding["provider"], LEGACY_PACKAGE_PIN_PROVIDERS)
        legacy = copy.deepcopy(binding)
        legacy["provider"] = next(iter(LEGACY_PACKAGE_PIN_PROVIDERS))
        validation = validate_package_pin_binding(
            self.schedule,
            self.platform,
            self.positions,
            self.plan,
            self.anchors,
            self.bsp,
            legacy,
        )
        self.assertEqual(validation["status"], "pass")

    def test_binding_corruption_is_rejected(self) -> None:
        binding = self._build()
        broken = copy.deepcopy(binding)
        broken["entries"][0]["source_package_pin"] = "WRONG"
        with self.assertRaisesRegex(ValidationError, "independently agree"):
            validate_package_pin_binding(
                self.schedule,
                self.platform,
                self.positions,
                self.plan,
                self.anchors,
                self.bsp,
                broken,
            )

    def test_electrically_incompatible_bsp_is_infeasible(self) -> None:
        incompatible = self._bsp("LVCMOS12")
        with self.assertRaisesRegex(EmuFlowError, "no complete electrically"):
            build_package_pin_binding(
                self.schedule,
                self.platform,
                self.positions,
                self.plan,
                self.anchors,
                incompatible,
                executable=str(self.bsp_solver),
                iostandard="LVCMOS18",
            )

    def test_checked_in_synthetic_bsp_is_reproducible(self) -> None:
        platform_path = (
            ROOT / "platforms/virtual/xcvu9p_4fpga_mesh.json"
        )
        checked = (
            ROOT
            / "platforms/synthetic/xcvu9p_4fpga_mesh_bsp.json"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            generated = Path(temporary_directory) / "bsp.json"
            subprocess.run(
                [
                    "python3",
                    str(
                        ROOT
                        / "scripts/generate_synthetic_vu9p_bsp.py"
                    ),
                    str(platform_path),
                    str(generated),
                ],
                check=True,
            )
            self.assertEqual(checked.read_bytes(), generated.read_bytes())
        validation = validate_hardware_bsp(
            read_json(checked), Platform.load(platform_path)
        )
        self.assertEqual(validation["pins"], 512)
        self.assertEqual(validation["channels"], 256)
        self.assertEqual(validation["domains"], 8)

    def test_source_backed_mps4_gty_binding_projects_user_bits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            boarddb = Path(temporary_directory) / "mps4.json"
            materialize_arm_mps4_boarddb(
                boarddb,
                name="mps4_serial_binding_fixture",
                fabric_clock_mhz=50.0,
                payload_bits_per_lane_per_cycle=64,
                latency_cycles=4,
            )
            platform = Platform.load(boarddb)
        schedule = {
            "design": "mps4_serial_binding_fixture",
            "platform": platform.name,
            "schema":"emuflow.tdm-schedule/v1",
            "provider":"physical-binding-fixture-v1",
            "metrics":{"frame_slots":16,"completion_slot":7},
            "routes":[{"id":f"d{index}","net":f"net{index}","source":"mps4_1",
                       "sinks":["mps4_2"],"tree_edges":[{"link":"mps4_b2b_1","from":"mps4_1","to":"mps4_2"}]}
                      for index in range(4)],
            "demand_completions":[{"demand":f"d{index}","net":f"net{index}",
                "source_ready_slot":0,"completion_slot":index+4,"sink_arrival_slots":{"mps4_2":index+4}}
                for index in range(4)],
            "domain_schedules":[],
            "entries": [
                {
                    "id": f"serial-entry-{index}",
                    "demand":f"d{index}","net":f"net{index}","hop":0,
                    "ready_slot":0,"arrival_slot":index+4,
                    "capacity_key":"mps4_b2b_1:mps4_1-to-mps4_2",
                    "link": "mps4_b2b_1",
                    "from": "mps4_1",
                    "to": "mps4_2",
                    "tdm_ratio": 4,
                    "slot": index,
                    "lane": index,
                }
                for index in range(4)
            ],
        }
        positions = {
            "schema": "emuflow.signal-position-hints/v1",
            "design": schedule["design"],
            "platform": platform.name,
            "provider": "openparf-lookahead-centroid-v1",
            "region_count": 1,
            "metrics": {"signals": 4, "endpoint_centroid_fallbacks": 0},
            "entries": [
                {
                    "schedule_entry": entry["id"],
                    "source_y": 0.5,
                    "sink_y": 0.5,
                    "source_region": 0,
                    "sink_region": 0,
                    "source_fallback": False,
                    "sink_fallback": False,
                }
                for entry in schedule["entries"]
            ],
        }
        plan = make_pin_plan_fixture(
            schedule,
            platform,
            positions,
            assignments={entry["id"]: (0, 0) for entry in schedule["entries"]},
        )
        endpoints = {fpga.id: [] for fpga in platform.fpgas}
        for item in plan["entries"]:
            logical_lane = item["physical_lane"]
            for fpga, peer, kind in (
                ("mps4_1", "mps4_2", "tx"),
                ("mps4_2", "mps4_1", "rx"),
            ):
                endpoints[fpga].append(
                    {
                        "id": (
                            f"__emuflow_{kind}_{item['schedule_entry']}"
                        ),
                        "link": "mps4_b2b_1",
                        "peer": peer,
                        "kind": kind,
                        "lane": logical_lane,
                        "slot": next(
                            entry["slot"]
                            for entry in schedule["entries"]
                            if entry["id"] == item["schedule_entry"]
                        ),
                    }
                )
        anchors = {
            fpga.id: _build_virtual_anchors(
                fpga.id, platform, endpoints[fpga.id]
            )
            for fpga in platform.fpgas
        }
        binding = build_serial_transceiver_binding(
            schedule, platform, positions, plan, anchors
        )
        self.assertEqual(binding["provider"], SERIAL_TRANSCEIVER_PROVIDER)
        self.assertLessEqual(
            binding["metrics"]["directed_transceiver_channels"], 12
        )
        self.assertEqual(binding["metrics"]["logical_bindings"], 1)
        entry = binding["entries"][0]
        lane = entry["physical_lane"]
        source = platform.links[0].endpoint_binding("mps4_1")
        sink = platform.links[0].endpoint_binding("mps4_2")
        self.assertEqual(
            entry["source_package_pins"]["p"],
            source.lanes[lane].tx_package_pin_p,
        )
        self.assertEqual(
            entry["sink_package_pins"]["p"],
            sink.lanes[lane].rx_package_pin_p,
        )
        validation = validate_serial_transceiver_binding(
            schedule, platform, positions, plan, anchors, binding
        )
        self.assertEqual(validation["status"], "pass")
        xdc = binding_to_xdc(binding, "mps4_1")
        self.assertIn("gty_txp_mps4_b2b_1_mps4_2_lane", xdc)
        self.assertNotIn("set_property IOSTANDARD", xdc)
        corrupted = copy.deepcopy(binding)
        corrupted["entries"][0]["source_package_pins"]["p"] = "WRONG"
        with self.assertRaisesRegex(ValidationError, "independently agree"):
            validate_serial_transceiver_binding(
                schedule, platform, positions, plan, anchors, corrupted
            )
        default_endpoints = {fpga.id: [] for fpga in platform.fpgas}
        for entry in schedule["entries"]:
            for fpga, peer, kind in (
                ("mps4_1", "mps4_2", "tx"),
                ("mps4_2", "mps4_1", "rx"),
            ):
                default_endpoints[fpga].append(
                    {
                        "id": f"__emuflow_{kind}_{entry['id']}",
                        "link": entry["link"],
                        "peer": peer,
                        "kind": kind,
                        "lane": entry["lane"],
                        "slot": entry["slot"],
                    }
                )
        default_anchors = {
            fpga.id: _build_virtual_anchors(
                fpga.id, platform, default_endpoints[fpga.id]
            )
            for fpga in platform.fpgas
        }
        fixed_binding = build_serial_transceiver_binding(
            schedule, platform, None, None, default_anchors
        )
        self.assertEqual(fixed_binding["metrics"]["logical_bindings"], 4)
        self.assertEqual(
            fixed_binding["metrics"]["directed_transceiver_channels"], 1
        )
        self.assertEqual(
            fixed_binding["metrics"]["active_transceiver_sites"], 2
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = {
                "schedule": root / "schedule.json",
                "platform": root / "platform.json",
                "positions": root / "positions.json",
                "plan": root / "pin_plan.json",
            }
            write_tdm_schedule_storage(paths['schedule'],schedule)
            write_json(paths['platform'],platform.to_dict())
            anchor_paths = {}
            for fpga, document in default_anchors.items():
                path = root / f"{fpga}.anchors.json"
                write_json(path, document)
                anchor_paths[fpga] = path
            output = root / "phase6b"
            report = run_phase6b(
                schedule_path=paths["schedule"],
                platform_path=paths["platform"],
                positions_path=None,
                pin_plan_path=None,
                anchor_paths=anchor_paths,
                bsp_path=None,
                output_dir=output,
            )
            self.assertEqual(report["provider"], SERIAL_TRANSCEIVER_PROVIDER)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                read_json(output / "package_pin_binding.json"), fixed_binding
            )


if __name__ == "__main__":
    unittest.main()
