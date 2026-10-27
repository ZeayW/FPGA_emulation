from tests.physical_handoff_fixture import write_handoff_fixture
import copy
import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from unittest import mock
from pathlib import Path
from unittest.mock import patch

from tests.rwroute_reference import export_rwroute_reference, _qualified_lut_clock_gates
from emuflow.common.errors import ValidationError
from emuflow.physical.xilinx_netlist import (
    emit_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from emuflow.physical.xilinx_physical_handoff import (
    open_xilinx_physical_handoff,
)
from emuflow.physical.rapidwright.xilinx_rwroute import (
    _validate_rapidwright_device_data,
    export_rwroute_input,
    validate_xilinx_route_db,
)
from tests.xilinx_timing_reference import (
    build_xilinx_routed_timing,
    iter_xilinx_routed_timing_endpoints,
    validate_xilinx_routed_timing,
)
from emuflow.frontend.yosys import import_yosys_json


SHA = "0" * 64


class XilinxRWRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.native_build import emuflow_native
        cls.native_exporter = emuflow_native()

    def setUp(self):
        native = mock.patch(
            "emuflow.physical.rapidwright.rwroute_native.resolve_native_executable",
            return_value=str(self.native_exporter),
        )
        native.start()
        self.addCleanup(native.stop)

    def test_large_typed_export_never_reloads_cells_by_net(self):
        net_count = 100_000

        class NoCellAccess(Mapping):
            def __getitem__(self, _key):
                raise AssertionError("typed exporter reloaded a mapped cell")

            def __iter__(self):
                raise AssertionError("typed exporter scanned compatibility cells")

            def __len__(self):
                return 2 * net_count

        class TypedMapped(dict):
            def iter_physical_cells(self, _handoff):
                for index in range(net_count):
                    name = f"s{index:06d}"
                    yield name, "LUT1", f"SLICE_X0Y{2 * index}", "A6LUT"
                for index in range(net_count):
                    name = f"z{index:06d}"
                    yield name, "FDRE", f"SLICE_X0Y{2 * index + 1}", "AFF"

            def iter_physical_pins_by_net(self, _handoff):
                return iter(())

            def iter_physical_route_endpoints_by_net(
                self, _handoff, *, ordinal_offset=0,
            ):
                for index in range(net_count):
                    bit = index + 2
                    yield (
                        bit, f"s{index:06d}", "LUT1", "O", 0, 1,
                        "output", f"SLICE_X0Y{2 * index}",
                        ordinal_offset + index, "O",
                    )
                    yield (
                        bit, f"z{index:06d}", "FDRE", "D", 0, 1,
                        "input", f"SLICE_X0Y{2 * index + 1}",
                        ordinal_offset + net_count + index, "D",
                    )

            def iter_qualified_lut_clock_gates(self, *_args, **_kwargs):
                return iter(())

        mapped = TypedMapped({
            "modules": {"top": {"ports": {}, "cells": NoCellAccess()}},
        })
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff = root / "physical-handoff.sqlite"
            output = root / "route.tsv"
            report = export_rwroute_reference(
                root / "mapped.sqlite", handoff, handoff, output,
                mapped_value=mapped, packed_value=packed,
                placement_value=placement,
                source_sha256={
                    "mapped_sha256": "1" * 64,
                    "packed_sha256": "2" * 64,
                    "placement_sha256": "2" * 64,
                },
            )
            self.assertGreater(output.stat().st_size, 10_000_000)
        self.assertEqual(report["logical_cells"], 2 * net_count)
        self.assertEqual(report["routable_nets"], net_count)

    def test_typed_handoff_streams_route_ordinals_without_safe_map(self):
        mapped = {"modules": {"top": {
            "attributes": {"top": "1"}, "ports": {},
            "cells": {
                "a_src": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "attributes": {}, "port_directions": {"O": "output"},
                    "connections": {"O": [2]},
                },
                "z_sink": {
                    "type": "FDRE", "parameters": {}, "attributes": {},
                    "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            },
            "netnames": {"signal": {"bits": [2]}},
        }}}
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "mapping_profile": "test", "top": "top", "source": {},
            "policy": {}, "cascade_chains": [], "unplaced_constants": [],
            "clusters": [
                {
                    "id": "cluster0", "kind": "slice",
                    "site_templates": ["SLICEL"], "control_set": None,
                    "assignments": [{
                        "instance": "a_src", "cell_type": "LUT1",
                        "bel": "A6LUT",
                    }],
                },
                {
                    "id": "cluster1", "kind": "slice",
                    "site_templates": ["SLICEL"], "control_set": None,
                    "assignments": [{
                        "instance": "z_sink", "cell_type": "FDRE",
                        "bel": "AFF",
                    }],
                },
            ],
            "summary": {"clusters": 2},
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "status": "pass",
            "part": "xcvu19p-test", "provider": "test", "policy": {},
            "source": {}, "clusters": [
                {
                    "cluster": f"cluster{index}",
                    "site": f"SLICE_X0Y{index}", "site_type": "SLICEL",
                    "x": 0, "y": index, "fixed": False,
                    "physical_region": None,
                    "assignments": [{
                        "instance": name, "cell_type": cell_type,
                        "bel": bel, "placement_mode": "SLICEL",
                        "site": f"SLICE_X0Y{index}",
                    }],
                }
                for index, (name, cell_type, bel) in enumerate((
                    ("a_src", "LUT1", "A6LUT"),
                    ("z_sink", "FDRE", "AFF"),
                ))
            ],
            "summary": {"clusters": 2},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mapped.json"
            ir_path = root / "mapped.emuir.sqlite"
            mapped_index = root / "mapped.sqlite"
            handoff = root / "physical-handoff.sqlite"
            output = root / "route.tsv"
            source.write_text(json.dumps(mapped), encoding="utf-8")
            import_yosys_json(source).save(ir_path)
            emit_xilinx_mapped_index(ir_path, mapped_index)
            write_handoff_fixture(handoff, packed, placement)
            packed_view, placement_view = open_xilinx_physical_handoff(handoff)
            with (
                open_xilinx_mapped_index(mapped_index) as indexed,
                mock.patch.object(
                    indexed,
                    "iter_physical_cells",
                    wraps=indexed.iter_physical_cells,
                ) as physical_cells,
                mock.patch(
                    "tests.rwroute_reference.heapq.merge",
                    side_effect=AssertionError("safe-name map was built"),
                ),
                mock.patch.object(
                    type(indexed.cells), "items",
                    side_effect=AssertionError("all mapped cells were scanned"),
                ),
                mock.patch.object(
                    type(indexed.cells), "__getitem__",
                    side_effect=AssertionError("mapped cell JSON was decoded"),
                ),
            ):
                report = export_rwroute_input(mapped_index,
                    handoff,
                    output,
                    source_sha256={
                        "mapped_sha256": "1" * 64,
                        "packed_sha256": "2" * 64,
                        "placement_sha256": "2" * 64,
                    })
                self.assertEqual(physical_cells.call_count, 0)
            rows = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            report["runtime_breakdown"]["dataflow"]["safe_names"],
            "native-ordinal-cursor",
        )
        self.assertIn("CELL\tc0\ta_src\tLUT1\tSLICE_X0Y0\tA6LUT", rows)
        self.assertIn("CELL\tc1\tz_sink\tFDRE\tSLICE_X0Y1\tAFF", rows)
        self.assertIn("PIN\tn2\tc0\tO\tdriver", rows)
        self.assertIn("PIN\tn2\tc1\tD\tsink", rows)

    def test_exporter_routes_fixed_virtual_io_instead_of_excluding_top_ports(self):
        mapped = {"modules": {"top": {
            "ports": {
                "tx": {"direction": "output", "bits": [1]},
                "rx": {"direction": "input", "bits": [2]},
            },
            "cells": {
                "tx_src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "rx_sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            },
        }}}
        assignments = [
            {"instance": "tx_src", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "rx_sink", "cell_type": "FDRE", "bel": "AFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "tx_src", "bel": "A6LUT", "site": "SLICE_X0Y0"},
                {"instance": "rx_sink", "bel": "AFF", "site": "SLICE_X0Y1"},
            ]}],
        }
        def io(identity, direction, port, bit, p, n, ps, ns):
            return {
                "id": identity, "direction": direction, "logical_port": port,
                "logical_bit": bit, "logical_port_width": 1,
                "iostandard": "LVDS", "p": {"port": identity + "_p",
                "package_pin": p, "site": ps}, "n": {"port": identity + "_n",
                "package_pin": n, "site": ns},
            }
        contract = {
            "schema": "emuflow.virtual-io-physical-contract/v1", "status": "pass",
            "entries": [
                io("tx0", "tx", "tx", 0, "BR49", "BT49", "IOB_X0Y0", "IOB_X0Y1"),
                io("rx0", "rx", "rx", 0, "BW47", "BW48", "IOB_X0Y2", "IOB_X0Y3"),
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            report = export_rwroute_reference(
                *paths, output, virtual_io_contract=contract,
                source_sha256={
                    "mapped_sha256": SHA,
                    "packed_sha256": SHA,
                    "placement_sha256": SHA,
                },
            )
            text = output.read_text(encoding="utf-8")
            ir_path = root / "mapped.emuir.sqlite"
            mapped_index = root / "mapped.sqlite"
            import_yosys_json(paths[0]).save(ir_path)
            emit_xilinx_mapped_index(ir_path, mapped_index)
            indexed_output = root / "route-indexed.tsv"
            with open_xilinx_mapped_index(mapped_index) as indexed:
                indexed_report = export_rwroute_reference(
                    mapped_index, paths[1], paths[2], indexed_output,
                    mapped_value=indexed,
                    virtual_io_contract=contract,
                    source_sha256={
                        "mapped_sha256": SHA,
                        "packed_sha256": SHA,
                        "placement_sha256": SHA,
                    },
                )
            indexed_text = indexed_output.read_text(encoding="utf-8")
        self.assertEqual(report["virtual_io_cells"], 2)
        self.assertEqual(report["routable_nets"], 2)
        self.assertEqual(
            report["runtime_breakdown"]["dataflow"],
            {
                "route_cells": "generated-from-physical-binding",
                "physical_bindings": "compatibility-python-map",
                "safe_names": "ordered-merge-integer-index",
                "endpoints": "compatibility-python-index",
                "tsv": "streamed-atomic-spool",
            },
        )
        self.assertTrue(all(
            report["runtime_breakdown"][name] >= 0.0
            for name in (
                "binding_seconds", "cell_and_io_rows_seconds",
                "endpoint_index_seconds", "clock_validation_seconds",
                "net_rows_seconds", "atomic_commit_seconds",
                "total_seconds",
            )
        ))
        self.assertEqual(text.count("\nIOB\t"), 2)
        self.assertIn("\nNET\tn1\tsignal\n", text)
        self.assertIn("\nNET\tn2\tsignal\n", text)
        self.assertNotIn("boundary_or_driverless", text)
        self.assertEqual(
            indexed_report["routable_nets"], report["routable_nets"]
        )
        self.assertEqual(
            indexed_report["virtual_io_cells"], report["virtual_io_cells"]
        )
        self.assertNotIn("boundary_or_driverless", indexed_text)
        self.assertEqual(
            indexed_report["runtime_breakdown"]["dataflow"]["endpoints"],
            "typed-mapped-pin-stream",
        )

    def test_recovery_tree_drops_stable_connections_between_iterations(self):
        source = (
            Path(__file__).parents[1]
            / "scripts/rapidwright/EmuFlowRWRoute.java"
        ).read_text(encoding="utf-8")
        self.assertIn("recoveryConnections.removeIf(", source)
        self.assertIn("!super.shouldRoute(connection)", source)
        self.assertIn("EmuFlow active recovery connections", source)
        self.assertIn("MAX_RECOVERY_ITERATION_SECONDS = 600.0", source)
        self.assertIn("connection -> !connection.isRouted()", source)
        self.assertIn("recoveryRemaining >= recoveryAtStart", source)
        self.assertIn("unroutedRemaining >= unroutedAtStart", source)
        self.assertIn("without reducing its active", source)
        self.assertIn("refusing an unbounded recovery tail", source)

    def test_rapidwright_block_ram_aliases_match_yosys_ports(self):
        source = (
            Path(__file__).parents[1]
            / "scripts/rapidwright/EmuFlowRWRoute.java"
        ).read_text(encoding="utf-8")
        for yosys, rapidwright in (
            ("DOADO", "DOUTADOUT"),
            ("DOBDO", "DOUTBDOUT"),
            ("DOPADOP", "DOUTPADOUTP"),
            ("DOPBDOP", "DOUTPBDOUTP"),
            ("DIADI", "DINADIN"),
            ("DIBDI", "DINBDIN"),
            ("DIPADIP", "DINPADINP"),
            ("DIPBDIP", "DINPBDINP"),
        ):
            self.assertIn(
                f'case "{yosys}": port = "{rapidwright}";', source
            )
        self.assertIn("blockRamLogicalPin(cell, row[3])", source)

    def test_rapidwright_block_ram_bus_and_physical_wea_contract(self):
        source = (
            Path(__file__).parents[1]
            / "scripts/rapidwright/EmuFlowRWRoute.java"
        ).read_text(encoding="utf-8")
        self.assertIn('suffix = "[" + (index % 2) + "]";', source)
        self.assertIn('result.add("WEA" + index);', source)
        self.assertIn('result.add("WEA" + (index + 2));', source)
        self.assertIn("logicalNet.createPortInst(\n                port, index,", source)
        self.assertIn("blockRamLogicalPinOwners", source)
        self.assertIn("aliased logical pin belongs to different nets", source)

    def test_rapidwright_dsp_cascade_uses_physical_b_bus_site_pins(self):
        source = (
            Path(__file__).parents[1]
            / "scripts/rapidwright/EmuFlowRWRoute.java"
        ).read_text(encoding="utf-8")
        for logical, physical in (
            ("ACOUT", "ACOUT_B"),
            ("ACIN", "ACIN_B"),
            ("BCOUT", "BCOUT_B"),
            ("BCIN", "BCIN_B"),
        ):
            self.assertIn(
                f'physicalPin.replace("{logical}", "{physical}")', source
            )

    def test_rapidwright_writer_seals_pinned_timing_inputs_directly(self):
        source = (
            Path(__file__).parents[1]
            / "scripts/rapidwright/EmuFlowRWRoute.java"
        ).read_text(encoding="utf-8")
        for field, argument in (
            ("source_revision", 2),
            ("intersite_delay_terms.txt", 3),
            ("intrasite_delay_terms.txt", 4),
            ("data/parts.db", 5),
            ("data/devices/virtexuplus/xcvu19p_db.dat", 6),
        ):
            self.assertIn(f'"{field}"', source)
            self.assertIn(f"args[{argument}]", source)
        self.assertIn('"emuflow.xilinx-route-db/v2"', source)
        self.assertIn("DigestOutputStream", source)
        self.assertIn('"jsonl-object/v1"', source)
        self.assertIn('"jsonl-array/v1"', source)
        self.assertNotIn('output.put("nets"', source)

    def test_device_data_provider_fails_closed_on_unpinned_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data/parts.db"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not-the-pinned-provider")
            with self.assertRaisesRegex(ValidationError, "pinned XCVU19P"):
                _validate_rapidwright_device_data(root)

    def _route(self):
        return {
            "schema": "emuflow.xilinx-route-db/v1", "status": "candidate",
            "provider": "rapidwright-rwroute-2026.1.0", "part": "xcvu19p-test",
            "source": {
                "mapped_sha256": SHA, "packed_sha256": SHA,
                "placement_sha256": SHA, "rwroute_input_sha256": SHA,
            },
            "cells": 3,
            "materialization": {
                "route_cells": 3,
                "physical_cells": 3,
                "transformed_dsp48e2_cells": 0,
                "router": (
                    "CUFR-HUS-non-timing-driven-uturn-enabled-"
                    "parallel-unroutable-recovery-extended-lcb-clock"
                ),
            },
            "excluded_nets": [],
            "summary": {
                "candidate_nets": 1, "certificate_nets": 1,
                "static_nets": 0, "static_sinks": 0,
                "nets_with_pips": 1, "pips": 3, "excluded_nets": 0,
                "boundary_clock_nets": 0,
            },
            "timing": {
                "provider": "rapidwright-lightweight",
                "family": "UltraScalePlus", "units": "ps",
                "setup_route_delays": "available",
                "hold_analysis": "unavailable",
                "hard_block_clock_timing": "unqualified",
                "source_revision": "127f55cd704c277372697e699f1559e1cdc91f34",
                "source_data_sha256": {
                    "intersite_delay_terms.txt": "3b122837c4a1b5f3c212fc6353bee3a0854a204f2f69e2bc1cac4a2a1f9d7333",
                    "intrasite_delay_terms.txt": "ff08ce9041da649f2bf886d900f033dd23de8ad54eb4db55d37ce032dc0ec0a0",
                },
                "device_data_md5": {
                    "data/parts.db": "58dd6f20c37798322b6904a8a786a3de",
                    "data/devices/virtexuplus/xcvu19p_db.dat": "5ad01490fe442f360aa67d7dfe0fa1c3",
                },
                "logic_coefficients_ps": {
                    "ff_clock_to_q": 25.0, "carry_co": 10.0,
                    "lut_a1": 70.0, "lut_a2": 65.0,
                    "lut_a3": 60.0, "lut_a4": 55.0,
                    "lut_a5": 50.0, "lut_a6": 45.0,
                },
                "routed_endpoints": 2,
                "maximum_route_delay_ps": 27.5,
            },
            "nets": [{
                "net": "n1", "kind": "signal",
                "qualification": "ordinary-fabric-signal", "has_gap": False,
                "source_present": True, "sink_count": 2,
                "pins": [
                    {"site": "S0", "pin": "O", "is_output": True, "node": "A"},
                    {"site": "S1", "pin": "I", "is_output": False, "node": "B", "route_delay_ps": 12.0},
                    {"site": "S2", "pin": "I", "is_output": False, "node": "C", "route_delay_ps": 27.5},
                ],
                "pips": [
                    {"tile": "T0", "start_wire": "W0", "end_wire": "W1", "start_node": "A", "end_node": "X"},
                    {"tile": "T1", "start_wire": "W2", "end_wire": "W3", "start_node": "X", "end_node": "B"},
                    {"tile": "T2", "start_wire": "W4", "end_wire": "W5", "start_node": "X", "end_node": "C"},
                ],
            }],
        }

    def _stream_route(self, root: Path, value=None):
        value = copy.deepcopy(self._route() if value is None else value)
        nets = value.pop("nets")
        net_payload = root / "route.json.nets.jsonl"
        pip_payload = root / "route.json.pips.jsonl"
        net_lines = []
        pip_lines = []
        for net in nets:
            record = copy.deepcopy(net)
            pips = record.pop("pips")
            record["pip_count"] = len(pips)
            net_lines.append(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
            )
            for pip in pips:
                pip_lines.append(json.dumps([
                    net["net"], pip["tile"], pip["start_wire"],
                    pip["end_wire"], pip["start_node"], pip["end_node"],
                    pip.get("bidirectional", False), pip.get("reversed", False),
                ], separators=(",", ":")))
        net_payload.write_text("\n".join(net_lines) + "\n", encoding="utf-8")
        pip_payload.write_text("\n".join(pip_lines) + "\n", encoding="utf-8")
        value["schema"] = "emuflow.xilinx-route-db/v2"
        value["payloads"] = {
            "nets": {
                "format": "jsonl-object/v1", "path": net_payload.name,
                "sha256": hashlib.sha256(net_payload.read_bytes()).hexdigest(),
                "records": len(net_lines),
            },
            "pips": {
                "format": "jsonl-array/v1", "path": pip_payload.name,
                "sha256": hashlib.sha256(pip_payload.read_bytes()).hexdigest(),
                "records": len(pip_lines),
            },
        }
        path = root / "route.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, net_payload, pip_payload

    def test_checker_accepts_connected_tree_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(self._route()), encoding="utf-8")
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["nets"], 1)
            self.assertEqual(report["timed_endpoints"], 2)
            self.assertEqual(report["maximum_route_delay_ps"], 27.5)
            broken = self._route()
            broken["nets"][0]["pips"].pop()
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_xilinx_route_db(path)

    def test_indexed_checker_counts_cell_types_without_rebuilding_pins(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "sink_a": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
                "sink_b": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
            }}}
        }
        packed = {"schema": "emuflow.packed-site-netlist/v1", "top": "top"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped_json = root / "mapped.json"
            mapped_ir = root / "mapped.emuir.sqlite"
            mapped_index = root / "mapped.sqlite"
            packed_path = root / "packed.json"
            route_path = root / "route.json"
            mapped_json.write_text(json.dumps(mapped), encoding="utf-8")
            packed_path.write_text(json.dumps(packed), encoding="utf-8")
            route_path.write_text(json.dumps(self._route()), encoding="utf-8")
            import_yosys_json(mapped_json).save(mapped_ir)
            emit_xilinx_mapped_index(mapped_ir, mapped_index)
            with (
                open_xilinx_mapped_index(mapped_index) as indexed,
                mock.patch.object(
                    type(indexed.cells), "values",
                    side_effect=AssertionError("mapped pins were rebuilt"),
                ),
            ):
                report = validate_xilinx_route_db(
                    route_path,
                    mapped_path=mapped_index,
                    packed_path=packed_path,
                    mapped_value=indexed,
                    packed_value=packed,
                    source_sha256={
                        "mapped_sha256": SHA,
                        "packed_sha256": SHA,
                    },
                )
        self.assertEqual(report["status"], "pass")

    def test_checker_seals_exact_rwroute_input_for_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = self._route()
            input_path = root / "rwroute.tsv"
            input_path.write_text("NET\tn1\tsignal\n", encoding="utf-8")
            route["source"]["rwroute_input_sha256"] = hashlib.sha256(
                input_path.read_bytes()
            ).hexdigest()
            route_path = root / "route.json"
            route_path.write_text(json.dumps(route), encoding="utf-8")
            self.assertEqual(
                validate_xilinx_route_db(
                    route_path, rwroute_input_path=input_path
                )["status"],
                "pass",
            )
            input_path.write_text("NET\tn2\tsignal\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "routed input"):
                validate_xilinx_route_db(
                    route_path, rwroute_input_path=input_path
                )

    def test_streaming_checker_reads_sealed_payloads_without_monolithic_nets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, net_payload, pip_payload = self._stream_route(Path(temporary))
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("nets", manifest)
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["nets"], 1)
            self.assertEqual(report["pips"], 3)
            pip_payload.write_text(
                pip_payload.read_text(encoding="utf-8").replace('"T2"', '"T9"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "digest disagrees"):
                validate_xilinx_route_db(path)
            self.assertTrue(net_payload.is_file())

    def test_streaming_checker_rejects_truncation_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _net_payload, pip_payload = self._stream_route(root)
            pip_payload.write_bytes(pip_payload.read_bytes()[:-1])
            with self.assertRaisesRegex(ValidationError, "truncated record"):
                validate_xilinx_route_db(path)
            path, _net_payload, _pip_payload = self._stream_route(root)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["payloads"]["nets"]["path"] = "../escape.jsonl"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "payloads.nets is invalid"):
                validate_xilinx_route_db(path)

    def test_checker_never_rewrites_route_artifact(self):
        for status in ("candidate", "pass"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                value = self._route()
                value["status"] = status
                path = Path(temporary) / "route.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                original = path.read_bytes()
                report = validate_xilinx_route_db(path, _value=value)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(report["status"], "pass")
                self.assertNotIn("source_sha256", report)

    def test_checker_accepts_equivalent_alternate_physical_source(self):
        value = self._route()
        net = value["nets"][0]
        net["alternate_sources"] = [{
            "site": "S0", "pin": "OMUX", "is_output": True, "node": "A2",
        }]
        net["pips"][1]["start_node"] = "A2"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_xilinx_route_db(path)["nets"], 1)
            broken = copy.deepcopy(value)
            broken["nets"][0]["alternate_sources"][0]["is_output"] = False
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "alternate_sources"):
                validate_xilinx_route_db(path)

    def test_checker_accepts_device_tied_static_forest(self):
        value = self._route()
        value["nets"].append({
            "net": "GLOBAL_LOGIC1", "kind": "static_vcc",
            "qualification": "device-tied-static", "has_gap": False,
            "source_present": False, "sink_count": 2,
            "roots": ["VCC0", "VCC1"],
            "pins": [
                {"site": "S3", "pin": "A1", "is_output": False, "node": "D"},
                {"site": "S4", "pin": "CE", "is_output": False, "node": "E"},
            ],
            "pips": [
                {"tile": "T3", "start_wire": "W6", "end_wire": "W7", "start_node": "VCC0", "end_node": "D"},
                {"tile": "T4", "start_wire": "W8", "end_wire": "W9", "start_node": "VCC1", "end_node": "E"},
            ],
        })
        value["summary"].update({
            "certificate_nets": 2, "static_nets": 1, "static_sinks": 2,
            "nets_with_pips": 2, "pips": 5,
        })
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["static_nets"], 1)
            self.assertEqual(report["static_sinks"], 2)
            broken = copy.deepcopy(value)
            broken["nets"][1]["roots"] = ["NOT_CONNECTED"]
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "source-connected"):
                validate_xilinx_route_db(path)

    def test_checker_requires_explicit_clock_qualification(self):
        value = self._route()
        value["nets"][0]["kind"] = "clock"
        value["nets"][0]["qualification"] = "dedicated-bufgce-clock"
        value["materialization"]["dedicated_clock_gate_cells"] = 1
        value["clock_gates"] = [{
            "safe_cell": "c0", "logical_cell": "gate",
            "logical_type": "LUT2", "physical_type": "BUFGCE",
            "physical_site": "S0", "physical_bel": "BUFCE",
            "ce_type": "ASYNC",
            "qualification": "lut2-init-1000-fdre-latched-enable-v1",
        }]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_xilinx_route_db(path)["clock_nets"], 1)
            value["nets"][0]["qualification"] = (
                "dedicated-bufgce-clock-with-fabric-cascade-spur"
            )
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(validate_xilinx_route_db(path)["clock_nets"], 1)
            value["materialization"]["physical_cells"] += 1
            value["materialization"]["fabric_prefilter_clock_gate_cells"] = 1
            value["clock_gates"][0].update({
                "physical_type": "BUFGCE",
                "input_site": "S1",
                "ce_type": "ASYNC_TIED_HIGH",
                "implementation": "lut2-prefilter-bufgce-tied-high",
                "cascaded_input": True,
            })
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(
                validate_xilinx_route_db(path)[
                    "fabric_prefilter_clock_gate_cells"
                ],
                1,
            )
            value["nets"][0]["qualification"] = "fabric-routed-clock"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "clock qualification"):
                validate_xilinx_route_db(path)

    def test_exporter_qualifies_latched_lut2_clock_gate(self):
        mapped = {"modules": {"top": {"cells": {
            "enable": {
                "type": "FDRE", "port_directions": {"Q": "output"},
                "connections": {"Q": [2]},
            },
            "gate": {
                "type": "LUT2", "parameters": {"INIT": "1000"},
                "port_directions": {"I0": "input", "I1": "input", "O": "output"},
                "connections": {"I0": [1], "I1": [2], "O": [3]},
            },
            "sink": {
                "type": "FDRE", "port_directions": {"C": "input"},
                "connections": {"C": [3]},
            },
            "ram": {
                "type": "RAMB18E2",
                "port_directions": {
                    "CLKARDCLK": "input", "CLKBWRCLK": "input",
                },
                "connections": {
                    "CLKARDCLK": [3], "CLKBWRCLK": [3],
                },
            },
        }}}}
        assignments = [
            {"instance": "enable", "cell_type": "FDRE", "bel": "AFF"},
            {"instance": "gate", "cell_type": "LUT2", "bel": "B6LUT"},
            {"instance": "sink", "cell_type": "FDRE", "bel": "CFF"},
            {
                "instance": "ram", "cell_type": "RAMB18E2",
                "bel": "RAMB18E2_L",
            },
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
            "mapping_profile": "test", "top": "top", "source": {},
            "policy": {}, "cascade_chains": [], "unplaced_constants": [],
            "clusters": [
                {
                    "id": "cluster0", "kind": "slice",
                    "site_templates": ["SLICEL"], "control_set": None,
                    "assignments": assignments[:2],
                },
                {
                    "id": "cluster1", "kind": "slice",
                    "site_templates": ["SLICEL"], "control_set": None,
                    "assignments": assignments[2:3],
                },
                {
                    "id": "cluster2", "kind": "bram",
                    "site_templates": ["RAMB18"], "control_set": None,
                    "assignments": assignments[3:],
                },
            ],
            "summary": {"clusters": 3},
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "status": "pass",
            "part": "xcvu19p-test", "provider": "test", "policy": {},
            "source": {},
            "clusters": [
                {
                    "cluster": f"cluster{index}", "site": site,
                    "site_type": "SLICEL", "x": 0, "y": index,
                    "fixed": False, "physical_region": None,
                    "assignments": [{
                        **assignment, "site": site,
                        "placement_mode": "SLICEL",
                    } for assignment in cluster_assignments],
                }
                for index, (cluster_assignments, site) in enumerate(zip(
                    (assignments[:2], assignments[2:3], assignments[3:]),
                    ("SLICE_X0Y0", "SLICE_X0Y1", "RAMB18_X0Y0"),
                ))
            ],
            "summary": {"clusters": 3},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            source_sha256 = {
                "mapped_sha256": "1" * 64,
                "packed_sha256": "2" * 64,
                "placement_sha256": "2" * 64,
            }
            report = export_rwroute_reference(
                *paths, root / "route.tsv", source_sha256=source_sha256
            )
            text = (root / "route.tsv").read_text(encoding="utf-8")
            ir_path = root / "mapped.emuir.sqlite"
            mapped_index = root / "mapped.sqlite"
            handoff = root / "physical-handoff.sqlite"
            indexed_output = root / "route-indexed.tsv"
            import_yosys_json(paths[0]).save(ir_path)
            emit_xilinx_mapped_index(ir_path, mapped_index)
            write_handoff_fixture(handoff, packed, placement)
            packed_view, placement_view = open_xilinx_physical_handoff(handoff)
            with (
                open_xilinx_mapped_index(mapped_index) as indexed,
                mock.patch.object(
                    indexed, "iter_qualified_lut_clock_gates",
                    wraps=indexed.iter_qualified_lut_clock_gates,
                ) as qualify,
                mock.patch.object(
                    indexed, "iter_physical_route_endpoints_by_net",
                    wraps=indexed.iter_physical_route_endpoints_by_net,
                ) as routed_pins,
                mock.patch(
                    "tests.rwroute_reference._lut_clock_candidate_pins",
                    side_effect=AssertionError(
                        "typed qualification rebuilt Python candidates"
                    ),
                ),
            ):
                indexed_report = export_rwroute_input(mapped_index,
                    handoff,
                    indexed_output,
                    source_sha256=source_sha256)
                self.assertEqual(qualify.call_count, 0)
                self.assertEqual(routed_pins.call_count, 0)
            indexed_text = indexed_output.read_text(encoding="utf-8")
        self.assertEqual(report["qualified_clock_gates"], 1)
        self.assertEqual(indexed_report["qualified_clock_gates"], 1)
        self.assertEqual(indexed_report["routable_nets"], report["routable_nets"])
        self.assertEqual(indexed_report["excluded_nets"], report["excluded_nets"])
        self.assertEqual(indexed_text.count("\nCLOCK_GATE\t"), 1)
        self.assertIn("\nCLOCK_GATE\t", text)
        self.assertIn("\nNET\tn3\tclock\n", text)
        self.assertIn("\nNET\tn2\tsignal\n", text)

    def test_typed_clock_gate_qualification_preserves_closed_chain_pruning(self):
        for add_invalid_sink in (False, True):
            with self.subTest(add_invalid_sink=add_invalid_sink):
                cells = {
                    "enable0": {
                        "type": "FDRE", "port_directions": {"Q": "output"},
                        "connections": {"Q": [10]},
                    },
                    "enable1": {
                        "type": "FDRE", "port_directions": {"Q": "output"},
                        "connections": {"Q": [11]},
                    },
                    "gate0": {
                        "type": "LUT2", "parameters": {"INIT": "1000"},
                        "port_directions": {
                            "I0": "input", "I1": "input", "O": "output",
                        },
                        "connections": {"I0": [20], "I1": [10], "O": [21]},
                    },
                    "gate1": {
                        "type": "LUT2", "parameters": {"INIT": "1000"},
                        "port_directions": {
                            "I0": "input", "I1": "input", "O": "output",
                        },
                        "connections": {"I0": [21], "I1": [11], "O": [22]},
                    },
                    "clock_sink": {
                        "type": "FDRE", "port_directions": {"C": "input"},
                        "connections": {"C": [22]},
                    },
                }
                if add_invalid_sink:
                    cells["invalid_sink"] = {
                        "type": "FDRE", "port_directions": {"D": "input"},
                        "connections": {"D": [21]},
                    }
                endpoints = {}
                for name, cell in cells.items():
                    for port, bits in cell["connections"].items():
                        role = (
                            "driver"
                            if cell["port_directions"][port] in {"output", "inout"}
                            else "sink"
                        )
                        for index, bit in enumerate(bits):
                            pin = port if len(bits) == 1 else f"{port}[{index}]"
                            endpoints.setdefault(bit, []).append((name, pin, role))
                expected = _qualified_lut_clock_gates(cells, endpoints)
                self.assertEqual(
                    expected, set() if add_invalid_sink else {"gate0", "gate1"}
                )

                bels = ("AFF", "BFF", "A6LUT", "B6LUT", "CFF", "DFF")
                assignments = [
                    {"instance": name, "cell_type": cell["type"], "bel": bel}
                    for (name, cell), bel in zip(cells.items(), bels)
                ]
                packed = {
                    "schema": "emuflow.packed-site-netlist/v1",
                    "status": "pass", "mapping_profile": "test", "top": "top",
                    "source": {}, "policy": {}, "cascade_chains": [],
                    "unplaced_constants": [],
                    "clusters": [{
                        "id": "cluster0", "kind": "slice",
                        "site_templates": ["SLICEL"], "control_set": None,
                        "assignments": assignments,
                    }],
                    "summary": {"clusters": 1},
                }
                placement = {
                    "schema": "emuflow.xilinx-placement/v1", "status": "pass",
                    "part": "xcvu19p-test", "provider": "test", "policy": {},
                    "source": {},
                    "clusters": [{
                        "cluster": "cluster0", "site": "SLICE_X0Y0",
                        "site_type": "SLICEL", "x": 0, "y": 0,
                        "fixed": False, "physical_region": None,
                        "assignments": [{
                            **assignment, "site": "SLICE_X0Y0",
                            "placement_mode": "SLICEL",
                        } for assignment in assignments],
                    }],
                    "summary": {"clusters": 1},
                }
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "mapped.json"
                    ir_path = root / "mapped.emuir.sqlite"
                    mapped_index = root / "mapped.sqlite"
                    handoff = root / "handoff.sqlite"
                    source.write_text(
                        json.dumps({"modules": {"top": {"cells": cells}}}),
                        encoding="utf-8",
                    )
                    import_yosys_json(source).save(ir_path)
                    emit_xilinx_mapped_index(ir_path, mapped_index)
                    write_handoff_fixture(handoff, packed, placement)
                    with open_xilinx_mapped_index(mapped_index) as indexed:
                        actual = {
                            name for name, _ordinal in
                            indexed.iter_qualified_lut_clock_gates(handoff)
                        }
                self.assertEqual(actual, expected)

    def test_rapidwright_input_is_parsed_in_two_streaming_passes(self):
        source = (
            Path(__file__).parents[1]
            / "scripts/rapidwright/EmuFlowRWRoute.java"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Files.readAllLines", source)
        self.assertGreaterEqual(source.count("Files.newBufferedReader(inputPath"), 2)
        self.assertNotIn("Map<String, List<String[]>> pinRows", source)

    def test_rapidwright_heap_override_is_bounded_java_size(self):
        source = (
            Path(__file__).parents[1] / "src/python/emuflow/physical/rapidwright/xilinx_rwroute.py"
        ).read_text(encoding="utf-8")
        self.assertIn('EMUFLOW_RAPIDWRIGHT_MAX_HEAP", "32g"', source)
        self.assertIn('f"-Xmx{maximum_heap}"', source)

    def test_checker_requires_explicit_boundary_clock_qualification(self):
        value = self._route()
        value["excluded_nets"] = [{
            "net": "nclk", "reason": "boundary_clock",
            "qualification": "ideal-boundary-clock",
        }]
        value["summary"]["excluded_nets"] = 1
        value["summary"]["boundary_clock_nets"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            report = validate_xilinx_route_db(path)
            self.assertEqual(report["boundary_clock_nets"], 1)
            value["excluded_nets"][0]["qualification"] = "unknown"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "clock qualification"):
                validate_xilinx_route_db(path)

    def test_checker_rejects_cross_net_resource_conflict(self):
        value = self._route()
        other = copy.deepcopy(value["nets"][0])
        other["net"] = "n2"
        value["nets"].append(other)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_xilinx_route_db(path)

    def test_checker_rejects_missing_or_tampered_timing(self):
        value = self._route()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.json"
            del value["nets"][0]["pins"][1]["route_delay_ps"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "route delay"):
                validate_xilinx_route_db(path)
            value = self._route()
            value["timing"]["maximum_route_delay_ps"] = 99.0
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "maximum route delay"):
                validate_xilinx_route_db(path)

    def test_routed_timing_binds_site_pins_to_logical_endpoints(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "sink_a": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
                "sink_b": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
            }}}
        }
        packed = {"schema": "emuflow.packed-site-netlist/v1", "top": "top"}
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [
                {"site": "S0", "assignments": [{"instance": "src", "bel": "A6LUT"}]},
                {"site": "S1", "assignments": [{"instance": "sink_a", "bel": "AFF"}]},
                {"site": "S2", "assignments": [{"instance": "sink_b", "bel": "AFF"}]},
            ],
        }
        route = self._route()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped_path, packed_path, placement_path = (
                root / "mapped.json", root / "packed.json", root / "placement.json"
            )
            for path, value in (
                (mapped_path, mapped), (packed_path, packed),
                (placement_path, placement),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
            for key, path in (
                ("mapped_sha256", mapped_path), ("packed_sha256", packed_path),
                ("placement_sha256", placement_path),
            ):
                route["source"][key] = hashlib.sha256(path.read_bytes()).hexdigest()
            route_path, _net_payload, _pip_payload = self._stream_route(
                root, route
            )
            output = root / "routed-timing.json"
            from emuflow.physical.rapidwright import xilinx_rwroute
            from tests import xilinx_timing_reference as xilinx_timing

            real_timing_read = xilinx_timing.read_json
            real_rwroute_sha256 = xilinx_rwroute._sha256
            real_timing_sha256 = xilinx_timing._sha256
            with (
                mock.patch(
                    "tests.xilinx_timing_reference.read_json",
                    wraps=real_timing_read,
                ) as timing_read,
                mock.patch(
                    "emuflow.physical.rapidwright.xilinx_rwroute.read_json",
                    side_effect=AssertionError(
                        "route validator reparsed preloaded objects"
                    ),
                ),
                mock.patch(
                    "emuflow.physical.rapidwright.xilinx_rwroute._sha256",
                    wraps=real_rwroute_sha256,
                ) as rwroute_sha256,
                mock.patch(
                    "tests.xilinx_timing_reference._sha256",
                    wraps=real_timing_sha256,
                ) as timing_sha256,
            ):
                report = build_xilinx_routed_timing(
                    mapped_path, packed_path, placement_path, route_path, output
                )
            self.assertEqual(
                [call.args[0] for call in timing_read.call_args_list].count(
                    route_path
                ),
                1,
            )
            self.assertEqual(
                [call.args[0] for call in rwroute_sha256.call_args_list],
                [mapped_path, packed_path, placement_path, route_path],
            )
            self.assertEqual(
                [call.args[0] for call in timing_sha256.call_args_list],
                [output],
            )
            checked = validate_xilinx_routed_timing(
                output, mapped_path=mapped_path, packed_path=packed_path,
                placement_path=placement_path, route_path=route_path,
            )
            value = json.loads(output.read_text())
            endpoints = list(
                iter_xilinx_routed_timing_endpoints(output, value)
            )
            endpoint_payload = output.parent / value["payloads"]["endpoints"]["path"]
            endpoint_payload.write_text(
                endpoint_payload.read_text(encoding="utf-8").replace(
                    "0.012", "0.013", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "digest disagrees"):
                validate_xilinx_routed_timing(output)
        self.assertEqual(report["logical_endpoints"], 2)
        self.assertEqual(checked["physical_route_sinks"], 2)
        self.assertEqual(
            [item["route_delay_ns"] for item in endpoints],
            [0.012, 0.0275],
        )

    def test_routed_timing_reuses_exact_route_validation_seal(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "sink_a": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
                "sink_b": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [1]},
                },
            }}}
        }
        packed = {"schema": "emuflow.packed-site-netlist/v1", "top": "top"}
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [
                {"site": "S0", "assignments": [{"instance": "src", "bel": "A6LUT"}]},
                {"site": "S1", "assignments": [{"instance": "sink_a", "bel": "AFF"}]},
                {"site": "S2", "assignments": [{"instance": "sink_b", "bel": "AFF"}]},
            ],
        }
        route = self._route()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped_path, packed_path, placement_path = (
                root / "mapped.json", root / "packed.json", root / "placement.json"
            )
            for path, value in (
                (mapped_path, mapped), (packed_path, packed),
                (placement_path, placement),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
            for key, path in (
                ("mapped_sha256", mapped_path), ("packed_sha256", packed_path),
                ("placement_sha256", placement_path),
            ):
                route["source"][key] = hashlib.sha256(path.read_bytes()).hexdigest()
            route_path = root / "route.json"
            route_path.write_text(json.dumps(route), encoding="utf-8")
            validation = validate_xilinx_route_db(
                route_path, mapped_path=mapped_path, packed_path=packed_path,
                placement_path=placement_path,
            )
            output = root / "routed-timing.json"
            with patch(
                "tests.xilinx_timing_reference.validate_xilinx_route_db",
                side_effect=AssertionError("route validator ran twice"),
            ):
                report = build_xilinx_routed_timing(
                    mapped_path, packed_path, placement_path, route_path, output,
                    route_validation=validation,
                )
        self.assertEqual(report["logical_endpoints"], 2)

    def test_exporter_excludes_intra_site_net(self):
        mapped = {
            "modules": {"top": {"cells": {
                "a": {"type": "LUT1", "port_directions": {"O": "output"}, "connections": {"O": [1]}},
                "b": {"type": "FDRE", "port_directions": {"D": "input"}, "connections": {"D": [1]}},
            }}}
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": [
                {"instance": "a", "cell_type": "LUT1", "bel": "A6LUT"},
                {"instance": "b", "cell_type": "FDRE", "bel": "AFF"},
            ]}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "a", "bel": "A6LUT"},
                {"instance": "b", "bel": "AFF"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("mapped.json", "packed.json", "placement.json")]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            report = export_rwroute_reference(*paths, output)
            text = output.read_text(encoding="utf-8")
        self.assertEqual(report["routable_nets"], 0)
        self.assertIn("EXCLUDED\tn1\tintra_site", text)

    def test_exporter_rejects_two_routed_carry8_outputs_on_one_site_exit(self):
        mapped = {
            "modules": {"top": {"cells": {
                "carry": {
                    "type": "CARRY8",
                    "port_directions": {"CO": "output", "O": "output"},
                    "connections": {
                        "CO": ["x", 10, "x", "x", "x", "x", "x", "x"],
                        "O": ["x", 11, "x", "x", "x", "x", "x", "x"],
                    },
                },
                "co_sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [10]},
                },
                "o_sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [11]},
                },
            }}}
        }
        assignments = [
            {"instance": "carry", "cell_type": "CARRY8", "bel": "CARRY8"},
            {"instance": "co_sink", "cell_type": "FDRE", "bel": "AFF"},
            {"instance": "o_sink", "cell_type": "FDRE", "bel": "BFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "carry", "bel": "CARRY8", "site": "SLICE_X0Y0"},
                {"instance": "co_sink", "bel": "AFF", "site": "SLICE_X0Y1"},
                {"instance": "o_sink", "bel": "BFF", "site": "SLICE_X0Y2"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "CARRY8 shared site exit is multiply driven"
            ):
                export_rwroute_reference(*paths, root / "route.tsv")

    def test_exporter_qualifies_driverless_boundary_clock(self):
        mapped = {
            "modules": {"top": {"cells": {
                "ff": {
                    "type": "FDRE", "port_directions": {"C": "input"},
                    "connections": {"C": [1]},
                },
            }}}
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": [
                {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
            ]}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "ff", "bel": "AFF"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in ("mapped.json", "packed.json", "placement.json")]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            export_rwroute_reference(*paths, output)
            text = output.read_text(encoding="utf-8")
        self.assertIn(
            "EXCLUDED\tn1\tboundary_clock\tideal-boundary-clock", text
        )

    def test_exporter_reuses_preloaded_large_objects(self):
        mapped = {
            "modules": {"top": {"cells": {
                "ff": {
                    "type": "FDRE", "port_directions": {"C": "input"},
                    "connections": {"C": [1]},
                },
            }}}
        }
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": [
                {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
            ]}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "ff", "bel": "AFF"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "route.tsv"
            with mock.patch(
                "emuflow.physical.rapidwright.xilinx_rwroute.read_json",
                side_effect=AssertionError("preloaded objects were reparsed"),
            ):
                report = export_rwroute_reference(
                    root / "mapped.json",
                    root / "packed.json",
                    root / "placement.json",
                    output,
                    mapped_value=mapped,
                    packed_value=packed,
                    placement_value=placement,
                    source_sha256={
                        "mapped_sha256": "1" * 64,
                        "packed_sha256": "2" * 64,
                        "placement_sha256": "3" * 64,
                    },
                )
        self.assertEqual(report["logical_cells"], 1)

    def test_exporter_physically_expands_lut6_2_for_rapidwright(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src_di": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "src_s": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [2]},
                },
                "adapter": {
                    "type": "LUT6_2",
                    "port_directions": {
                        "I0": "input", "I1": "input", "I2": "input",
                        "I3": "input", "I4": "input", "I5": "input",
                        "O5": "output", "O6": "output",
                    },
                    "connections": {
                        "I0": [1], "I1": [2], "I2": ["0"],
                        "I3": ["0"], "I4": ["0"], "I5": ["1"],
                        "O5": [3], "O6": [4],
                    },
                },
                "sink_di": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [3]},
                },
                "sink_s": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [4]},
                },
            }}}
        }
        assignments = [
            {"instance": "src_di", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "src_s", "cell_type": "LUT1", "bel": "B6LUT"},
            {"instance": "adapter", "cell_type": "LUT6_2", "bel": "C6LUT"},
            {"instance": "sink_di", "cell_type": "FDRE", "bel": "AFF"},
            {"instance": "sink_s", "cell_type": "FDRE", "bel": "BFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        sites = {
            "src_di": "SLICE_X0Y0", "src_s": "SLICE_X0Y1",
            "adapter": "SLICE_X0Y2", "sink_di": "SLICE_X0Y3",
            "sink_s": "SLICE_X0Y4",
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{
                "site": "SLICE_X0Y2",
                "assignments": [
                    {"instance": item["instance"], "bel": item["bel"],
                     "site": sites[item["instance"]]}
                    for item in assignments
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            report = export_rwroute_reference(*paths, output)
            rows = [line.split("\t") for line in output.read_text().splitlines()]
        cell_rows = {row[2]: row for row in rows if row[0] == "CELL"}
        o5 = cell_rows["adapter$physical_o5"]
        o6 = cell_rows["adapter$physical_o6"]
        self.assertEqual((o5[3], o5[5]), ("LUT5", "C5LUT"))
        self.assertEqual((o6[3], o6[5]), ("LUT6", "C6LUT"))
        pin_rows = {(row[1], row[2], row[3], row[4]) for row in rows if row[0] == "PIN"}
        self.assertIn(("n1", o5[1], "I0", "sink"), pin_rows)
        self.assertIn(("n2", o6[1], "I1", "sink"), pin_rows)
        self.assertIn(("n3", o5[1], "O", "driver"), pin_rows)
        self.assertIn(("n4", o6[1], "O", "driver"), pin_rows)
        self.assertEqual(report["logical_cells"], 5)
        self.assertEqual(report["cells"], 6)
        self.assertEqual(report["expanded_lut6_2_cells"], 1)

    def test_exporter_accounts_for_rapidwright_dsp48e2_transform(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "multiply": {
                    "type": "DSP48E2",
                    "port_directions": {"A": "input", "P": "output"},
                    "connections": {
                        "A": [1] + ["0"] * 29,
                        "P": [2] + ["0"] * 47,
                    },
                },
                "sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            }}}
        }
        assignments = [
            {"instance": "src", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "multiply", "cell_type": "DSP48E2", "bel": "DSP_ALU"},
            {"instance": "sink", "cell_type": "FDRE", "bel": "AFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "src", "bel": "A6LUT", "site": "SLICE_X0Y0"},
                {"instance": "multiply", "bel": "DSP_ALU", "site": "DSP48E2_X0Y0"},
                {"instance": "sink", "bel": "AFF", "site": "SLICE_X0Y1"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            report = export_rwroute_reference(*paths, output)
            rows = [line.split("\t") for line in output.read_text().splitlines()]
        dsp_rows = [row for row in rows if row[0] == "CELL" and row[3] == "DSP48E2"]
        self.assertEqual(len(dsp_rows), 1)
        self.assertEqual(report["cells"], 3)
        self.assertEqual(report["logical_cells"], 3)
        self.assertEqual(report["transformed_dsp48e2_cells"], 1)
        self.assertEqual(report["physical_cells"], 10)

    def test_exporter_seals_ramb_mode_parameters_without_init_payloads(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "memory": {
                    "type": "RAMB36E2",
                    "parameters": {
                        "READ_WIDTH_A": "00001001",
                        "WRITE_WIDTH_B": "01001000",
                        "DOA_REG": "0",
                        "WRITE_MODE_B": "READ_FIRST",
                        "INIT_00": "1" * 256,
                    },
                    "port_directions": {"WEBWE": "input", "DOUTADOUT": "output"},
                    "connections": {
                        "WEBWE": [1] + ["0"] * 7,
                        "DOUTADOUT": [2] + ["0"] * 31,
                    },
                },
                "sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            }}}
        }
        assignments = [
            {"instance": "src", "cell_type": "LUT1", "bel": "A6LUT"},
            {"instance": "memory", "cell_type": "RAMB36E2", "bel": "RAMB36E2"},
            {"instance": "sink", "cell_type": "FDRE", "bel": "AFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "src", "bel": "A6LUT", "site": "SLICE_X0Y0"},
                {"instance": "memory", "bel": "RAMB36E2", "site": "RAMB36_X0Y0"},
                {"instance": "sink", "bel": "AFF", "site": "SLICE_X0Y1"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            export_rwroute_reference(*paths, output)
            rows = [line.split("\t") for line in output.read_text().splitlines()]
        memory_safe = next(
            row[1] for row in rows if row[0] == "CELL" and row[2] == "memory"
        )
        parameters = {
            (row[2], row[3]) for row in rows
            if row[0] == "PARAM" and row[1] == memory_safe
        }
        self.assertIn(("READ_WIDTH_A", "9"), parameters)
        self.assertIn(("WRITE_WIDTH_B", "72"), parameters)
        self.assertIn(("DOA_REG", "0"), parameters)
        self.assertIn(("WRITE_MODE_B", "READ_FIRST"), parameters)
        self.assertFalse(any(row[0] == "PARAM" and row[2].startswith("INIT") for row in rows))

    def test_exporter_keeps_ramb18_vector_pin_identity(self):
        mapped = {
            "modules": {"top": {"cells": {
                "src": {
                    "type": "LUT1", "port_directions": {"O": "output"},
                    "connections": {"O": [1]},
                },
                "memory": {
                    "type": "RAMB18E2",
                    "port_directions": {
                        "ADDRARDADDR": "input", "DOADO": "output",
                    },
                    "connections": {
                        "ADDRARDADDR": [1] + ["0"] * 13,
                        "DOADO": [2] + ["0"] * 15,
                    },
                },
                "sink": {
                    "type": "FDRE", "port_directions": {"D": "input"},
                    "connections": {"D": [2]},
                },
            }}}
        }
        assignments = [
            {"instance": "src", "cell_type": "LUT1", "bel": "A6LUT"},
            {
                "instance": "memory", "cell_type": "RAMB18E2",
                "bel": "RAMB18E2_U",
            },
            {"instance": "sink", "cell_type": "FDRE", "bel": "AFF"},
        ]
        packed = {
            "schema": "emuflow.packed-site-netlist/v1", "top": "top",
            "clusters": [{"assignments": assignments}],
        }
        placement = {
            "schema": "emuflow.xilinx-placement/v1", "part": "xcvu19p-test",
            "clusters": [{"site": "SLICE_X0Y0", "assignments": [
                {"instance": "src", "bel": "A6LUT", "site": "SLICE_X0Y0"},
                {
                    "instance": "memory", "bel": "RAMB18E2_U",
                    "site": "RAMB18_X0Y1",
                },
                {"instance": "sink", "bel": "AFF", "site": "SLICE_X0Y1"},
            ]}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / name for name in (
                "mapped.json", "packed.json", "placement.json"
            )]
            for path, value in zip(paths, (mapped, packed, placement)):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = root / "route.tsv"
            export_rwroute_reference(*paths, output)
            rows = [line.split("\t") for line in output.read_text().splitlines()]
        memory_safe = next(
            row[1] for row in rows if row[0] == "CELL" and row[2] == "memory"
        )
        memory_pins = {
            row[3] for row in rows
            if row[0] == "PIN" and row[2] == memory_safe
        }
        self.assertEqual(memory_pins, {"ADDRARDADDR[0]", "DOADO[0]"})


if __name__ == "__main__":
    unittest.main()
