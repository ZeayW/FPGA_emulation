from contextlib import closing
import copy
import hashlib
import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.common.errors import ValidationError
from emuflow.scheduling.tdm_schedule_storage import TDM_SCHEDULE_LOGICAL_SCHEMA
from emuflow.transport.chimew.chimew_binding_storage import (
    ChimewBindingEntrySequence,
    write_chimew_binding_storage,
)
from emuflow.common.io import read_json, write_json
from emuflow.ir.ir import EmuIR
from emuflow.flow.multi_fpga_physical_flow import (
    MULTI_FPGA_PHYSICAL_SCHEMA,
    _assemble_multi_fpga_physical_results,
    _implementation_stage,
    _indexed_emuir_resource_totals,
    _open_position_hint_lookup,
    _partition_has_physical_work,
    _record_chimew_fixed_io_target,
    _retire_consumed_physical_input,
    _validate_transport_resource_reservation,
    run_multi_fpga_physical_flow,
    validate_multi_fpga_physical_report,
)
from emuflow.physical.physical_summary_storage import (
    PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA,
    read_physical_summary,
)
from emuflow.physical.physical_backend import physical_backend_descriptor
from emuflow.platform.platform import Platform
from emuflow.transport.position_hints_storage import write_position_hints_storage


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _merged_ir(fpga):
    instances = [
        {
            "id": f"{fpga}_original",
            "name": f"{fpga}_original",
            "type": "LUT1",
            "resources": {"lut": 1},
            "parameters": {"INIT": "10"},
            "attributes": {},
            "constant_connections": [
                {"port": "I0", "bit": 0, "value": "0"}
            ],
        },
        {
            "id": f"{fpga}_transport",
            "name": f"{fpga}_transport",
            "type": "LUT1",
            "resources": {"lut": 1},
            "parameters": {"INIT": "10"},
            "attributes": {},
            "constant_connections": [
                {"port": "I0", "bit": 0, "value": "1"}
            ],
        },
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": f"design__{fpga}",
                "top": f"design__{fpga}",
                "source_format": "test",
            },
            "ports": [],
            "instances": instances,
            "nets": [],
            "clocks": [],
            "warnings": [],
        }
    )


class MultiFpgaPhysicalFlowTest(unittest.TestCase):
    def test_mapped_owner_remains_consumable_after_placement_input_retirement(self):
        from emuflow.physical.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            placement, mapped = root / "placement.emuir.sqlite", root / "mapped.sqlite"
            _merged_ir("fpga0").save(placement)
            report = emit_xilinx_mapped_index(placement, mapped)
            _retire_consumed_physical_input(
                placement, {"status": "pass", "output": str(placement)},
                last_consumer="mapped-export-test",
            )
            self.assertFalse(placement.exists())
            with open_xilinx_mapped_index(mapped) as value:
                cells = value["modules"][report["top"]]["cells"]
                self.assertEqual(set(cells), {"fpga0_original", "fpga0_transport"})
                self.assertEqual([cells[name]["type"] for name in sorted(cells)], ["LUT1", "LUT1"])

    def test_consumed_input_retirement_rejects_unfinished_or_unowned_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, source = root / "placement.emuir.sqlite", root / "source.sqlite"
            path.write_bytes(b"completed derived input")
            source.write_bytes(b"shared source")
            report = {"status": "pass", "output": str(source)}
            with self.assertRaises(ValidationError):
                _retire_consumed_physical_input(path, report, last_consumer="test")
            self.assertTrue(path.is_file())
            report["output"] = str(path)
            for status in ("running", "fail"):
                report["status"] = status
                with self.assertRaises(ValidationError):
                    _retire_consumed_physical_input(path, report, last_consumer="test")
                self.assertTrue(path.is_file())
            report["status"] = "pass"
            for suffix in ("-wal", "-journal"):
                journal = Path(str(path) + suffix)
                journal.write_bytes(b"unclosed writer")
                with self.assertRaises(ValidationError):
                    _retire_consumed_physical_input(path, report, last_consumer="test")
                self.assertTrue(path.is_file())
                journal.unlink()
            path.unlink()
            path.symlink_to(source)
            with self.assertRaises(ValidationError):
                _retire_consumed_physical_input(path, report, last_consumer="test")
            self.assertEqual(source.read_bytes(), b"shared source")

    def test_transport_resources_must_fit_sealed_phase3_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transport.emuir.sqlite"
            _merged_ir("fpga0").save(path)
            resources = _indexed_emuir_resource_totals(path)
        self.assertEqual(resources, {"lut": 2})
        report = _validate_transport_resource_reservation(
            "fpga0", resources, {"fpga0": {"lut": 2}}
        )
        self.assertEqual(report["headroom"], {"lut": 0})
        self.assertFalse(report["automatic_relaxation"])
        with self.assertRaisesRegex(
            ValidationError, "sealed Phase 3 reservation"
        ):
            _validate_transport_resource_reservation(
                "fpga0", resources, {"fpga0": {"lut": 1}}
            )

    def test_indexed_position_hint_lookup_does_not_expand_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "position-hints.json"
            positions = write_position_hints_storage(
                path,
                {
                    "schema": "emuflow.signal-position-hints/v1",
                    "design": "design",
                    "platform": "platform",
                    "provider": "test",
                    "region_count": 3,
                    "metrics": {
                        "signals": 2,
                        "endpoint_centroid_fallbacks": 0,
                    },
                    "entries": [
                        {
                            "schedule_entry": entry_id,
                            "source_y": 0.25,
                            "sink_y": 0.75,
                            "source_region": 0,
                            "sink_region": 2,
                            "source_fallback": False,
                            "sink_fallback": False,
                        }
                        for entry_id in ("entry-a", "entry-b")
                    ],
                },
            )

            class EntriesMustStayLazy:
                def __iter__(self):
                    raise AssertionError("indexed entries were expanded")

            positions._entries = EntriesMustStayLazy()
            with _open_position_hint_lookup(positions) as has_hint:
                self.assertTrue(has_hint("entry-b"))
                self.assertTrue(has_hint("entry-a"))
                self.assertFalse(has_hint("entry-missing"))

    def test_only_exactly_empty_partitions_skip_physical_work(self):
        netlist = {"instances": 0, "nets": 0, "ports": 0}
        transport = {
            "endpoints": [],
            "source_signals": [],
            "shadow_signals": [],
        }
        self.assertFalse(_partition_has_physical_work(netlist, transport))

        relay = copy.deepcopy(transport)
        relay["endpoints"] = [{"id": "relay-rx"}]
        self.assertTrue(_partition_has_physical_work(netlist, relay))

        passthrough = copy.deepcopy(netlist)
        passthrough["ports"] = 1
        self.assertTrue(_partition_has_physical_work(passthrough, transport))

        with self.assertRaisesRegex(ValidationError, "inventory"):
            _partition_has_physical_work(
                {"instances": None, "nets": 0, "ports": 0}, transport
            )

    def test_backend_specific_implementation_stage_owner(self):
        item = {
            "stages": {
                "local_path_timing": {"owner": "inactive"},
                "rapidwright_implementation": {
                    "local_path_timing": {"owner": "rapidwright"}
                },
                "vivado_implementation": {
                    "local_path_timing": {"owner": "vivado"}
                },
            }
        }
        with self.assertRaisesRegex(ValidationError, "unsupported physical backend"):
            _implementation_stage(item, "open", "local_path_timing")
        self.assertEqual(
            _implementation_stage(item, "rapidwright", "local_path_timing")[
                "owner"
            ],
            "rapidwright",
        )
        self.assertEqual(
            _implementation_stage(item, "vivado", "local_path_timing")[
                "owner"
            ],
            "vivado",
        )

    def test_validator_accepts_complete_rapidwright_stage(self):
        implementation = {
            "status": "pass",
            "mapped_netlist": {},
            "packing": {},
            "placement": {},
            "route": {},
            "routed_timing": {},
            "opensta": {},
            "boundary_timing": {},
        }
        report = {
            "schema": MULTI_FPGA_PHYSICAL_SCHEMA,
            "status": "pass",
            "backend": physical_backend_descriptor("rapidwright"),
            "expected_fpgas": ["fpga0"],
            "fpgas": [{
                "fpga": "fpga0",
                "status": "pass",
                "part": "xcvu19p-fsva3824-2-e",
                "original_cells": 1,
                "transport_cells": 1,
                "stages": {
                    "transport_synthesis": {"status": "pass"},
                    "placement_ir": {
                        "status": "pass",
                        "instances": 2,
                        "boundary_identity": {
                            "validation": {"status": "pass"}
                        },
                    },
                    "rapidwright_implementation": implementation,
                },
                "physical_result": {
                    "timing": {"critical_path_ns": 1.25}
                },
            }],
            "physical_summary_ref": "physical-summary.json",
        }
        with patch(
            "emuflow.flow.multi_fpga_physical_flow."
            "validate_physical_partition_result"
        ):
            validation = validate_multi_fpga_physical_report(report)
        self.assertEqual(validation["backend"], "rapidwright")
        self.assertEqual(validation["worst_critical_path_ns"], 1.25)

        del implementation["route"]
        with self.assertRaisesRegex(
            ValidationError, "RapidWright physical stages"
        ), patch(
            "emuflow.flow.multi_fpga_physical_flow."
            "validate_physical_partition_result"
        ):
            validate_multi_fpga_physical_report(report)

    def test_validator_keeps_inactive_coverage_without_backend_run(self):
        active = {
            "fpga": "fpga0",
            "status": "pass",
            "active": True,
            "part": "xcvu19p-fsva3824-2-e",
            "original_cells": 1,
            "transport_cells": 1,
            "stages": {
                "transport_synthesis": {"status": "pass"},
                "placement_ir": {
                    "status": "pass",
                    "instances": 2,
                    "boundary_identity": {
                        "validation": {"status": "pass"}
                    },
                },
                "rapidwright_implementation": {
                    "status": "pass",
                    **{
                        name: {}
                        for name in (
                            "mapped_netlist",
                            "packing",
                            "placement",
                            "route",
                            "routed_timing",
                            "opensta",
                            "boundary_timing",
                        )
                    },
                },
            },
            "physical_result": {"timing": {"critical_path_ns": 1.0}},
        }
        inactive = {
            "fpga": "fpga1",
            "status": "pass",
            "active": False,
            "part": "xcvu19p-fsva3824-2-e",
            "original_cells": 0,
            "transport_cells": 0,
            "stages": {
                "transport_synthesis": {"status": "pass"},
                "placement_ir": {
                    "status": "pass",
                    "instances": 0,
                    "boundary_identity": {
                        "validation": {"status": "pass"}
                    },
                },
                "boundary_timing": {"status": "pass"},
            },
            "physical_result": {
                "implementation_status": "inactive",
                "timing": {"critical_path_ns": 0.0},
            },
        }
        report = {
            "schema": MULTI_FPGA_PHYSICAL_SCHEMA,
            "status": "pass",
            "backend": physical_backend_descriptor("rapidwright"),
            "expected_fpgas": ["fpga0", "fpga1"],
            "fpgas": [active, inactive],
            "physical_summary_ref": "physical-summary.json",
        }
        with patch(
            "emuflow.flow.multi_fpga_physical_flow."
            "validate_physical_partition_result"
        ):
            validation = validate_multi_fpga_physical_report(report)
        self.assertEqual(validation["active_fpgas"], 1)
        self.assertEqual(validation["inactive_fpgas"], 1)

        inactive["stages"]["rapidwright_implementation"] = {
            "status": "pass"
        }
        with self.assertRaisesRegex(ValidationError, "inactive physical"), patch(
            "emuflow.flow.multi_fpga_physical_flow."
            "validate_physical_partition_result"
        ):
            validate_multi_fpga_physical_report(report)

    def test_chimew_fixed_io_targets_allow_only_intragroup_tdm_sharing(self):
        targets = {}
        packed_groups = {}
        group_packed_blocks = {}
        for _slot in range(2):
            _record_chimew_fixed_io_target(
                targets,
                packed_groups,
                group_packed_blocks,
                packed_name="packed_io0",
                group="group0",
                target_y=0.25,
            )
        self.assertEqual(targets, {"packed_io0": 0.25})

        with self.assertRaisesRegex(ValidationError, "different signal groups"):
            _record_chimew_fixed_io_target(
                targets,
                packed_groups,
                group_packed_blocks,
                packed_name="packed_io0",
                group="group1",
                target_y=0.25,
            )
        with self.assertRaisesRegex(ValidationError, "split across"):
            _record_chimew_fixed_io_target(
                targets,
                packed_groups,
                group_packed_blocks,
                packed_name="packed_io1",
                group="group0",
                target_y=0.25,
            )

    def test_every_partition_is_bound_through_checked_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            manifest = {
                "schema": "emuflow.split-manifest/v1",
                "design": "design",
                "platform": "academic_vtr_2fpga_p2p",
                "provider": "test",
                "board_binding": {"status": "virtual"},
                "clock_periods": {"clk": 10.0},
                "fpgas": [],
                "lane_map": "lane_map.json",
                "runtime_controller_rtl": "virtual_runtime_controller.sv",
            }
            (split / "virtual_runtime_controller.sv").write_text(
                "module emuflow_virtual_runtime_controller; endmodule\n",
                encoding="utf-8",
            )
            for fpga in ("fpga0", "fpga1"):
                fpga_root = split / fpga
                fpga_root.mkdir()
                write_json(
                    fpga_root / "transport.json",
                    {
                        "schema": "emuflow.transport-endpoints/v1",
                        "design": "design",
                        "platform": "academic_vtr_2fpga_p2p",
                        "fpga": fpga,
                        "endpoints": [],
                    },
                )
                (fpga_root / "transport_schedule.sv").write_text(
                    f"module emuflow_transport_{fpga}; endmodule\n",
                    encoding="utf-8",
                )
                manifest["fpgas"].append(
                    {
                        "fpga": fpga,
                        "netlist": "split.sqlite",
                        "netlist_selector": fpga,
                        "transport": f"{fpga}/transport.json",
                        "transport_rtl": f"{fpga}/transport_schedule.sv",
                        "virtual_anchors": f"{fpga}/virtual_anchors.json",
                    }
                )
            # Deliberately reverse the producer order; the physical report is
            # required to follow the BoardDB order even under concurrency.
            manifest["fpgas"].reverse()
            write_json(split / "manifest.json", manifest)
            write_json(root / "schedule.json", {"schema": TDM_SCHEDULE_LOGICAL_SCHEMA})
            architecture = root / "architecture.xml"
            architecture.write_text("<architecture/>\n", encoding="utf-8")

            runtime = {
                "design": "design",
                "fabric_clock": {"period_ns": 4.0},
                "virtual_dut_clock": {"nominal_period_ns": 128.0},
                "virtual_dut_clocks": {
                    "clk": {"nominal_period_ns": 128.0}
                },
                "timing_model": {"fabric_to_dut_max_delay_ns": 8.0},
            }

            worker_barrier = threading.Barrier(2, timeout=5.0)

            def fake_yosys(_sources, _top, consumer, **_kwargs):
                worker_barrier.wait()
                return consumer(io.StringIO(""))

            def fake_indexed_import(_source, output, **kwargs):
                from emuflow.ir.emuir_storage import write_emuir_sqlite

                write_emuir_sqlite(
                    output,
                    _merged_ir("transport").value,
                    source_bindings=kwargs.get("source_bindings", ()),
                    _finalize=kwargs.get("_finalize_emuir"),
                )

            class SplitCounts:
                def __init__(self, path, fpga):
                    self.fpga = fpga
                    self.assert_path = path
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def counts(self):
                    return {"instances": 1, "nets": 0, "ports": 1}

            def fake_lower(_split, fpga, _transport, _transport_ir, output, report):
                self.assertEqual(_split.resolve(), (split / "split.sqlite").resolve())
                self.assertTrue(_transport_ir.is_file())
                value = _merged_ir(fpga)
                value.save(output)
                boundary_path = output.with_name("boundary-identities.json")
                boundary = {
                    "schema": "emuflow.boundary-identity/v1",
                    "status": "pass",
                    "design": "design",
                    "platform": "academic_vtr_2fpga_p2p",
                    "fpga": fpga,
                    "provider": "test",
                    "coverage": {
                        "endpoints": 0,
                        "tx": 0,
                        "rx": 0,
                        "external_port_nets": 0,
                    },
                    "endpoints": [],
                }
                from emuflow.timing.boundary_identity_storage import (
                    write_boundary_identity_storage,
                )

                header = {key: boundary[key] for key in (
                    "schema", "status", "design", "platform", "fpga", "provider",
                )}
                write_boundary_identity_storage(
                    boundary_path, header, boundary["endpoints"]
                ).close()
                result = {
                    "schema": "emuflow.placement-ir-report/v1",
                    "status": "pass",
                    "design": f"design__{fpga}",
                    "instances": 2,
                    "nets": 0,
                    "resource_totals": {"lut": 2},
                    "transport_instances": 1,
                    "output": str(output),
                    "boundary_identity": {
                        "schema": "emuflow.boundary-identity/v1",
                        "output": str(boundary_path),
                        "validation": {
                            "status": "pass",
                            "fpga": fpga,
                            "endpoints": 0,
                            "tx": 0,
                            "rx": 0,
                        },
                    },
                }
                write_json(report, result)
                return result

            def fake_mapped_verilog(_ir, output, _report):
                output.write_text("// mocked mapped Verilog; no vendor execution\n")
                return {"status": "pass", "output": str(output)}

            def fake_vivado(**kwargs):
                from emuflow.timing.boundary_identity_storage import read_boundary_identity_storage
                from emuflow.timing.boundary_timing import boundary_timing_header
                from tests.timing_facts import boundary_timing_facts
                from emuflow.ir.resource_budget import validate_device_resource_usage
                self.assertFalse(kwargs["ir_path"].with_name("transport.emuir.sqlite").exists())
                self.assertTrue(kwargs["ir_path"].is_file())
                self.assertTrue(kwargs["mapped_verilog_path"].is_file())
                output = kwargs["output_dir"]
                output.mkdir(parents=True)
                timing_path = output / "boundary-timing.json"
                with read_boundary_identity_storage(kwargs["boundary_identity_path"]) as identity:
                    header = boundary_timing_header(identity, provider="test", qualification="mocked")
                    boundary_timing_facts(timing_path, header, ()).close()
                result = {
                    "schema": "emuflow.physical-partition-result/v1", "status": "pass",
                    "identity": {"backend": "vivado", "fpga": kwargs["fpga"], "part": kwargs["part"]},
                    "cell_accounting": {"original_cells": 1, "transport_cells": 1,
                        "routed_cells": 2, "physical_cells": 2, "infrastructure_cells": 0,
                        "optimization_cells": 0},
                    "closure": {"unrouted_nets": 0, "drc_violations": 0},
                    "clocks": {"fabric_period_ns": 4.0, "dut_period_ns": 128.0},
                    "timing": {"wns_ns": -14.0, "tns_ns": -14.0,
                        "failing_endpoints": 1, "failing_endpoint_constraints": 1,
                        "timing_met": False, "dut_wns_ns": 110.0, "fabric_wns_ns": -14.0,
                        "fabric_to_dut_wns_ns": -10.0, "critical_path_ns": 18.0},
                    "resource_usage": {"physical": validate_device_resource_usage(
                        {**{field: 0 for field in kwargs["runtime"]["resource_budget"]["capacity"]}, "lut": 2}, kwargs["runtime"]["resource_budget"], stage="post-physical-materialization")},
                    "artifacts": {"boundary_timing": {"path": str(timing_path), "sha256": _sha256(timing_path)}},
                }
                return {"status": "pass", "result": result,
                    "boundary_timing": {"status": "pass", "import": {"output": str(timing_path)}}}

            with (
                patch("emuflow.flow.multi_fpga_physical_flow.SplitNetlistView", SplitCounts),
                patch("emuflow.flow.multi_fpga_physical_flow.emit_vivado_mapped_verilog", side_effect=fake_mapped_verilog),
                patch(
                    "emuflow.flow.multi_fpga_physical_flow.build_virtual_runtime",
                    return_value=runtime,
                ),
                patch(
                    "emuflow.flow.multi_fpga_physical_flow."
                    "run_generic_yosys_stream",
                    side_effect=fake_yosys,
                ),
                patch(
                    "emuflow.flow.multi_fpga_physical_flow."
                    "import_yosys_json_indexed",
                    side_effect=fake_indexed_import,
                ),
                patch(
                    "emuflow.flow.multi_fpga_physical_flow."
                    "run_placement_ir_lowering_from_split",
                    side_effect=fake_lower,
                ),
                patch(
                    "emuflow.flow.multi_fpga_physical_flow.run_indexed_vivado_partition",
                    side_effect=fake_vivado,
                ),
            ):
                report = run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    workers=2,
                    backend="vivado",
                )
                physical_summary = read_json(
                    root / "physical/physical-summary.json"
                )
                for record in report["fpgas"]:
                    for stage in ("transport_synthesis", "placement_ir", "mapped_verilog"):
                        producer = record["stages"][stage]
                        self.assertFalse(Path(producer["output"]).exists())
                        self.assertEqual(producer["intermediate_lifecycle"]["state"], "retired")
                        self.assertGreater(producer["intermediate_lifecycle"]["bytes"], 0)
                    persisted = read_json(root / "physical" / record["fpga"] / "lowering/placement-ir-report.json")
                    self.assertEqual(persisted, record["stages"]["placement_ir"])
                self.assertTrue(all(
                    item["schema"] == PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA
                    for item in physical_summary["boundary_identities"].values()
                ))
                self.assertTrue(all(
                    item["schema"] == PHYSICAL_INDEXED_EVIDENCE_REFERENCE_SCHEMA
                    for item in physical_summary["boundary_timing"].values()
                ))
                lazy_summary = read_physical_summary(
                    root / "physical/physical-summary.json"
                )
                self.assertEqual(
                    lazy_summary["boundary_identities"]["fpga0"]["schema"],
                    "emuflow.boundary-identity/v1",
                )
                self.assertEqual(
                    lazy_summary["boundary_timing"]["fpga0"]["schema"],
                    "emuflow.boundary-timing/v1",
                )
                lazy_summary.close()
                # The same checked assembler must consume separately completed
                # records without re-entering any synthesis/place/route tool.
                assembly_inputs = dict(
                    records=list(reversed(report["fpgas"])),
                    manifest=manifest, manifest_path=(split / "manifest.json").resolve(),
                    platform=Platform.load(PLATFORM), runtime=runtime,
                    split_root=split, backend_descriptor=report["backend"],
                    architecture_source=report["architecture"],
                    execution=report["execution"], include_logic_timing=False,
                )
                with patch(
                    "emuflow.flow.multi_fpga_physical_flow."
                    "run_generic_yosys_stream",
                    side_effect=AssertionError("synthesis rerun forbidden"),
                ), patch(
                    "emuflow.flow.multi_fpga_physical_flow.run_indexed_vivado_partition",
                    side_effect=AssertionError("routing rerun forbidden"),
                ):
                    assembled = _assemble_multi_fpga_physical_results(
                        **assembly_inputs, output_dir=root / "assembled",
                    )
                self.assertEqual(assembled, report)
                self.assertEqual(
                    read_json(root / "assembled/physical-summary.json"),
                    physical_summary,
                )
                with patch(
                    "emuflow.flow.multi_fpga_physical_flow.validate_multi_fpga_physical_report",
                    side_effect=ValidationError("terminal report rejected"),
                ), self.assertRaisesRegex(ValidationError, "terminal report rejected"):
                    _assemble_multi_fpga_physical_results(
                        **assembly_inputs, output_dir=root / "rejected",
                    )
                self.assertFalse((root / "rejected/physical-summary.json").exists())
                self.assertFalse((root / "rejected/multi-fpga-physical-flow-report.json").exists())

                worker_barrier = threading.Barrier(1, timeout=5.0)
                batch = run_multi_fpga_physical_flow(
                    split, PLATFORM, root / "schedule.json", root / "worker",
                    workers=2, backend="vivado",
                    fpga_ids=["fpga1"],
                )
                self.assertEqual(batch["schema"], "emuflow.physical-partition-batch/v1")
                self.assertEqual(batch["selected_fpgas"], ["fpga1"])
                self.assertEqual(batch["expected_fpgas"], ["fpga0", "fpga1"])
                self.assertEqual(batch["execution"]["effective_workers"], 1)
                self.assertFalse((root / "worker/fpga0").exists())
                self.assertFalse((root / "worker/physical-summary.json").exists())
                self.assertFalse((root / "worker/multi-fpga-physical-flow-report.json").exists())
                worker_record = read_json(Path(batch["records"][0]["path"]))
                self.assertEqual(worker_record["fpga"], "fpga1")
                self.assertEqual(batch["records"][0]["sha256"], _sha256(Path(batch["records"][0]["path"])))
                joined = _assemble_multi_fpga_physical_results(
                    **{**assembly_inputs, "records": [worker_record, report["fpgas"][0]]},
                    output_dir=root / "joined-workers",
                )
                self.assertEqual(joined["summary"]["fpgas"], 2)
                # A failed last consumer leaves its input available for failure
                # diagnosis; neither another worker nor the shared split is retired.
                worker_barrier = threading.Barrier(1, timeout=5.0)
                with patch(
                    "emuflow.flow.multi_fpga_physical_flow.run_indexed_vivado_partition",
                    side_effect=ValidationError("physical implementation rejected"),
                ), self.assertRaisesRegex(ValidationError, "physical implementation rejected"):
                    run_multi_fpga_physical_flow(
                        split, PLATFORM, root / "schedule.json", root / "failed-physical",
                        backend="vivado", fpga_ids=["fpga0"],
                    )
                self.assertTrue((root / "failed-physical/fpga0/lowering/placement.emuir.sqlite").is_file())
                self.assertTrue((root / "failed-physical/fpga0/partition.v").is_file())
                self.assertFalse((root / "failed-physical/fpga0/partition-record.json").exists())
                with patch(
                    "emuflow.flow.multi_fpga_physical_flow.run_placement_ir_lowering_from_split",
                    side_effect=ValidationError("lowering rejected"),
                ), self.assertRaisesRegex(ValidationError, "lowering rejected"):
                    run_multi_fpga_physical_flow(
                        split, PLATFORM, root / "schedule.json", root / "failed-lowering",
                        backend="vivado", fpga_ids=["fpga0"],
                    )
                self.assertTrue((root / "failed-lowering/fpga0/transport.emuir.sqlite").is_file())
                self.assertTrue((split / "manifest.json").is_file())

        self.assertEqual(report["summary"]["fpgas"], 2)
        self.assertEqual(report["summary"]["original_cells"], 2)
        self.assertEqual(report["summary"]["transport_cells"], 2)
        self.assertNotIn("physical_summary", report)
        self.assertEqual(report["physical_summary_ref"], "physical-summary.json")
        self.assertEqual(
            [item["fpga"] for item in report["fpgas"]],
            ["fpga0", "fpga1"],
        )
        self.assertEqual(report["execution"]["requested_workers"], 2)
        self.assertEqual(report["execution"]["effective_workers"], 2)
        self.assertFalse(report["execution"]["pack_place_resume"])
        self.assertFalse(report["execution"]["route_resume"])
        self.assertNotIn("rapidwright_placer", report["execution"])
        self.assertEqual(
            validate_multi_fpga_physical_report(
                report,
                physical_summary,
            )["status"],
            "pass",
        )

    def test_completed_result_assembly_rejects_partial_failed_and_duplicate_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = dict(
                manifest={}, manifest_path=root / "manifest.json",
                platform=Platform.load(PLATFORM), runtime={}, split_root=root,
                output_dir=root / "out", backend_descriptor=physical_backend_descriptor("vivado"),
                architecture_source={}, execution={}, include_logic_timing=True,
            )
            for records in (
                [{"fpga": "fpga0", "status": "pass"}],
                [{"fpga": "fpga0", "status": "pass"}] * 2,
                [{"fpga": "fpga0", "status": "failed"}],
            ):
                with self.subTest(records=records), self.assertRaises(ValidationError), patch(
                    "emuflow.flow.multi_fpga_physical_flow.read_json",
                    side_effect=AssertionError("partial result must fail before artifact reads"),
                ):
                    _assemble_multi_fpga_physical_results(records=records, **common)
                self.assertFalse((root / "out/physical-summary.json").exists())

    def test_completed_resource_budget_cannot_be_omitted_or_reused_from_looser_platform(self):
        from emuflow.ir.resource_budget import validate_device_resource_usage
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_platform = Platform.load(PLATFORM)
            strict_platform = old_platform.with_utilization_limit(0.25)
            records = [
                {"fpga": fpga.id, "status": "pass", "physical_result": {}}
                for fpga in strict_platform.fpgas
            ]
            common = dict(
                manifest={}, manifest_path=root / "manifest.json",
                platform=strict_platform, runtime={}, split_root=root,
                output_dir=root / "out", backend_descriptor=physical_backend_descriptor("rapidwright"),
                architecture_source={}, execution={}, include_logic_timing=False,
            )
            with self.assertRaisesRegex(ValidationError, "resource budget evidence.*missing"):
                _assemble_multi_fpga_physical_results(records=records, **common)
            for fpga, record in zip(old_platform.fpgas, records):
                usage = validate_device_resource_usage(
                    {field: 0 for field in fpga.capacity}, fpga.to_dict(),
                    stage="post-physical-materialization",
                )
                record["physical_result"]["resource_usage"] = {"physical": usage}
            with self.assertRaisesRegex(ValidationError, "resource budget evidence.*disagrees"):
                _assemble_multi_fpga_physical_results(records=records, **common)
            self.assertFalse((root / "out/physical-summary.json").exists())

    def test_completed_fixed_io_cannot_omit_implementation_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = dict(
                manifest={"virtual_board_binding": {"binding": "binding.json"}},
                manifest_path=root / "manifest.json",
                platform=Platform.load(PLATFORM), runtime={}, split_root=root,
                output_dir=root / "out", architecture_source={}, execution={},
                include_logic_timing=True,
            )
            records = [
                {"fpga": fpga, "status": "pass", "stages": {}}
                for fpga in ("fpga0", "fpga1")
            ]
            for backend in ("vivado", "rapidwright"):
                with self.subTest(backend=backend), self.assertRaisesRegex(
                    ValidationError, "fixed differential I/O"
                ), patch(
                    "emuflow.flow.multi_fpga_physical_flow.read_json",
                    side_effect=AssertionError("missing I/O must fail before artifact reads"),
                ):
                    _assemble_multi_fpga_physical_results(
                        **common, records=records,
                        backend_descriptor=physical_backend_descriptor(backend),
                    )
            self.assertFalse((root / "out/physical-summary.json").exists())

    def test_worker_rejects_unknown_duplicate_and_empty_fpga_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            write_json(split / "manifest.json", {"schema": "emuflow.split-manifest/v1", "fpgas": []})
            write_json(root / "schedule.json", {"schema": TDM_SCHEDULE_LOGICAL_SCHEMA})
            for selected in ([], ["unknown"], ["fpga0", "fpga0"], "fpga0"):
                with self.subTest(selected=selected), self.assertRaisesRegex(
                    ValidationError, "FPGA selection"
                ):
                    run_multi_fpga_physical_flow(
                        split, PLATFORM, root / "schedule.json", root / "out",
                        backend="vivado", fpga_ids=selected,
                    )
            self.assertFalse((root / "out").exists())

    def test_rejects_non_positive_worker_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValidationError, "workers"):
                run_multi_fpga_physical_flow(
                    Path(temporary),
                    PLATFORM,
                    Path(temporary) / "schedule.json",
                    Path(temporary) / "physical",
                    workers=0,
                )

    def test_json_split_lowering_is_rejected_before_any_tool_starts(self):
        for selector in (None, "fpga1"):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                item = {"fpga": "fpga0", "netlist": "netlist.json"}
                if selector is not None:
                    item["netlist_selector"] = selector
                write_json(root / "manifest.json", {
                    "schema": "emuflow.split-manifest/v1", "fpgas": [item],
                })
                with patch("emuflow.flow.multi_fpga_physical_flow.Platform.load",
                           side_effect=AssertionError("old split must fail before tool/config loading")), \
                     self.assertRaisesRegex(ValidationError, "JSON netlists are retired"):
                    run_multi_fpga_physical_flow(root, PLATFORM, root / "schedule.json",
                                                root / "physical", backend="vivado")
                self.assertFalse((root / "physical").exists())

    def test_retired_rapidwright_placer_is_not_a_python_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(TypeError, "rapidwright_placer"):
                run_multi_fpga_physical_flow(
                    root,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    rapidwright_placer="legacy",
                )

    def test_rapidwright_default_is_openparf_native_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(
                ValidationError, "openparf-native requires source-sealed"
            ):
                run_multi_fpga_physical_flow(
                    root,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    backend="rapidwright",
                )

    def test_retired_arch_producers_are_not_cli_commands(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from emuflow.flow.cli import _build_parser

        parser = _build_parser()
        for command in ("pack-xilinx", "place-xilinx", "guide-xilinx-openparf",
                        "plan-xilinx-single-slr", "validate-xilinx-single-slr-plan"):
            with self.subTest(command=command):
                error = StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit):
                    parser.parse_args(["arch", command])
                self.assertIn("invalid choice", error.getvalue())

    def test_retired_rapidwright_placer_is_not_a_cli_option(self):
        from emuflow.flow.cli import _build_parser

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(SystemExit):
                _build_parser().parse_args([
                    "multi-fpga", "physical", "--split", str(root),
                    "--platform", str(PLATFORM), "--schedule", str(root / "schedule.json"),
                    "--out", str(root / "physical"), "--rapidwright-placer", "legacy",
                ])

    def test_rejects_partial_chimew_physical_anchor_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            manifest = {
                "schema": "emuflow.split-manifest/v1",
                "design": "design",
                "platform": "academic_vtr_2fpga_p2p",
                "fpgas": [],
                "runtime_controller_rtl": "runtime.sv",
                "electrical_binding": "electrical_binding.json",
            }
            write_json(split / "manifest.json", manifest)
            write_json(split / "electrical_binding.json", {"bindings": []})
            write_json(root / "schedule.json", {"schema": TDM_SCHEDULE_LOGICAL_SCHEMA})
            with self.assertRaisesRegex(ValidationError, "position hints"):
                run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    backend="vivado",
                )

    def test_indexed_chimew_selector_is_read_lazily_and_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            write_json(
                split / "manifest.json",
                {
                    "schema": "emuflow.split-manifest/v1",
                    "electrical_binding": "electrical_binding.json",
                    "fpgas": [],
                },
            )
            indexed = write_chimew_binding_storage(
                split / "electrical_binding.json",
                {
                    "schema": "emuflow.chimew-phase6-electrical-binding/v2",
                    "design": "design",
                    "platform": "academic_vtr_2fpga_p2p",
                    "fpga_y_bounds": [
                        {"fpga": "fpga0", "y_min": 0.0, "y_max": 1.0},
                        {"fpga": "fpga1", "y_min": 0.0, "y_max": 1.0},
                    ],
                    "entries": [
                        {
                            "group": "group0",
                            "link": "link0",
                            "fpga_a": "fpga0",
                            "fpga_b": "fpga1",
                            "physical_lane": 0,
                            "direction": "a_to_b",
                            "package_pin_a": "A1",
                            "package_pin_b": "B1",
                            "pin_a_point": {"x": 0.0, "y": 0.25},
                            "pin_b_point": {"x": 1.0, "y": 0.75},
                            "schedule_entries": ["entry0"],
                        }
                    ],
                },
            )
            write_json(root / "schedule.json", {"schema": TDM_SCHEDULE_LOGICAL_SCHEMA})
            with patch.object(
                ChimewBindingEntrySequence,
                "__iter__",
                side_effect=AssertionError("physical flow expanded typed binding"),
            ), self.assertRaisesRegex(ValidationError, "position hints"):
                run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    backend="vivado",
                )

            with closing(sqlite3.connect(indexed.store_path)) as connection, connection:
                connection.execute(
                    "UPDATE members SET member='tampered' WHERE member='entry0'"
                )
            with self.assertRaisesRegex(ValidationError, "store seal"):
                run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical-tampered",
                    backend="vivado",
                )

    def test_nonempty_output_requires_explicit_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "split"
            split.mkdir()
            write_json(
                split / "manifest.json",
                {
                    "schema": "emuflow.split-manifest/v1",
                    "clock_periods": {"clk": 10.0},
                    "fpgas": [{"fpga": "fpga0", "netlist_selector": "fpga0"}, {"fpga": "fpga1", "netlist_selector": "fpga1"}],
                },
            )
            output = root / "physical"
            output.mkdir()
            (output / "partial").write_text("partial\n", encoding="utf-8")
            write_json(root / "schedule.json", {"schema": TDM_SCHEDULE_LOGICAL_SCHEMA})
            runtime = {
                "fabric_clock": {"period_ns": 4.0},
                "virtual_dut_clock": {"nominal_period_ns": 128.0},
                "timing_model": {"fabric_to_dut_max_delay_ns": 8.0},
            }
            with (
                patch(
                    "emuflow.flow.multi_fpga_physical_flow.build_virtual_runtime",
                    return_value=runtime,
                ),
                self.assertRaisesRegex(
                    Exception, "physical output must be an empty directory"
                ),
            ):
                run_multi_fpga_physical_flow(
                    split,
                    PLATFORM,
                    root / "schedule.json",
                    output,
                    backend="vivado",
                )

    def test_rejects_logic_database_without_complete_original_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(
                ValidationError, "complete original STA path database"
            ):
                run_multi_fpga_physical_flow(
                    root,
                    PLATFORM,
                    root / "schedule.json",
                    root / "physical",
                    logic_path_database_path=root / "cut-paths.json",
                    backend="vivado",
                )


if __name__ == "__main__":
    unittest.main()
