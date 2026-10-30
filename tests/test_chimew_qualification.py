from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.chimew_qualification_fixture import build_chimew_phase6_qualification, validate_chimew_phase6_qualification
from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage
from tests.chimew_bank_fixture import build_chimew_phase6_pin_plan
from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
from contextlib import closing
from tests.native_build import emuflow_native
import copy
import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from emuflow.transport.chimew.chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
)
from emuflow.transport.chimew.chimew_bank_channel_storage import (
    write_chimew_bank_channel_storage,
)
from emuflow.transport.chimew.chimew_bank_channel_report_storage import (
    read_chimew_bank_channel_report,
)
from emuflow.transport.chimew.chimew_binding_storage import read_chimew_binding
from emuflow.flow.cli import main
from emuflow.transport.chimew.chimew_grouping import (
    CHIMEW_CROSSING_PROVIDER,
    CHIMEW_CROSSING_SCHEMA,
    build_chimew_initial_groups,
)
from emuflow.transport.chimew.chimew_qualification import (
    CHIMEW_QUALIFICATION_PROVIDER,
    canonical_sha256,
    validate_chimew_qualification_seal,
)
from emuflow.transport.chimew.chimew_phase6 import CHIMEW_ELECTRICAL_MAP_PROVIDER, CHIMEW_ELECTRICAL_MAP_SCHEMA
from tests.pin_contract_fixture import validate_chimew_phase6_binding
from emuflow.transport.chimew.chimew_pipeline import (
    CHIMEW_PIPELINE_PROVIDER,
    CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER,
    read_chimew_phase6_pipeline,
    run_chimew_phase6_pipeline,
    validate_chimew_phase6_pipeline,
)
from emuflow.transport.chimew.chimew_refinement import (
    CHIMEW_POSITION_PROVIDER,
    CHIMEW_POSITION_SCHEMA,
    refine_chimew_groups,
)
from emuflow.transport.chimew.chimew_rudy import (
    CHIMEW_RUDY_INPUT_PROVIDER,
    CHIMEW_RUDY_INPUT_SCHEMA,
    evaluate_chimew_rudy,
)
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json
from emuflow.transport.pin_plan_storage import read_pin_plan
from emuflow.platform.platform import Platform
from emuflow.transport.position_hints_storage import read_position_hints


ROOT = Path(__file__).resolve().parents[1]


class ChimewQualificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.executables = {name: str(emuflow_native()) for name in ("grouper", "refiner", "rudy", "assigner")}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.schedule = {
            "routes": [], "demand_completions": [], "domain_schedules": [],
            "schema": "emuflow.tdm-schedule/v1",
            "design": "qualification_fixture",
            "platform": "two_fpga",
            "entries": [
                {
                    "id": "s0",
                    "link": "AB",
                    "from": "A",
                    "to": "B",
                    "tdm_ratio": 1,
                    "lane": 0,
                    "slot": 0,
                },
                {
                    "id": "s1",
                    "link": "AB",
                    "from": "B",
                    "to": "A",
                    "tdm_ratio": 1,
                    "lane": 0,
                    "slot": 0,
                },
            ],
        }
        for index, entry in enumerate(self.schedule["entries"]):
            entry.update(demand=f"d{index}", net=f"n{index}", hop=0,
                         capacity_key=f"{entry['link']}:{entry['from']}:{entry['to']}",
                         ready_slot=entry["slot"], arrival_slot=entry["slot"]+1)
        self.crossings = {
            "schema": CHIMEW_CROSSING_SCHEMA,
            "provider": CHIMEW_CROSSING_PROVIDER,
            "design": self.schedule["design"],
            "platform": self.schedule["platform"],
            "slls_per_fpga": 2,
            "provenance": {
                "producer": "fixture-router",
                "producer_version": "1",
                "routing_sha256": "a" * 64,
            },
            "metrics": {"signals": 2, "physical_sll_crossings": 2},
            "entries": [
                {
                    "schedule_entry": "s0",
                    "source_slls": [0],
                    "sink_slls": [],
                    "encoding": 1,
                },
                {
                    "schedule_entry": "s1",
                    "source_slls": [1],
                    "sink_slls": [],
                    "encoding": 2,
                },
            ],
        }
        self.positions = {
            "schema": CHIMEW_POSITION_SCHEMA,
            "provider": CHIMEW_POSITION_PROVIDER,
            "design": self.schedule["design"],
            "platform": self.schedule["platform"],
            "coordinate_system": "physical-site-y",
            "provenance": {
                "producer": "fixture-lookahead",
                "producer_version": "1",
                "placement_sha256": "b" * 64,
            },
            "metrics": {"signals": 2},
            "entries": [
                {"schedule_entry": "s0", "source_y": 10.0},
                {"schedule_entry": "s1", "source_y": 50.0},
            ],
        }
        self.initial = build_chimew_initial_groups(
            self.schedule, self.crossings, executable=self.executables["grouper"]
        )
        self.refined = refine_chimew_groups(
            self.schedule,
            self.crossings,
            self.initial,
            self.positions,
            executable=self.executables["refiner"],
        )
        self.rudy_input = {
            "schema": CHIMEW_RUDY_INPUT_SCHEMA,
            "provider": CHIMEW_RUDY_INPUT_PROVIDER,
            "design": self.schedule["design"],
            "platform": self.schedule["platform"],
            "coordinate_system": "physical-site-xy",
            "degenerate_bbox_policy": "reject",
            "wire_pitch_per_layer": 1.0,
            "max_utilization": 1.0,
            "provenance": {
                "producer": "fixture-lookahead",
                "producer_version": "1",
                "placement_sha256": "b" * 64,
                "netlist_sha256": "d" * 64,
                "architecture_sha256": "c" * 64,
            },
            "grid": {
                "origin_x": 0.0,
                "origin_y": 0.0,
                "bin_width": 100.0,
                "bin_height": 100.0,
                "columns": 1,
                "rows": 1,
                "capacities": [1000.0],
            },
            "metrics": {"nets": 1, "pins": 2},
            "nets": [
                {
                    "id": "n0",
                    "pins": [{"x": 10.0, "y": 10.0}, {"x": 90.0, "y": 90.0}],
                }
            ],
        }
        self.rudy_report = evaluate_chimew_rudy(
            self.rudy_input, executable=self.executables["rudy"]
        )
        group_by_signal = {
            record["schedule_entry"]: record["group"]
            for record in self.refined["entries"]
        }
        self.bank_input = {
            "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
            "design": self.schedule["design"],
            "platform": self.schedule["platform"],
            "coordinate_system": "physical-site-xy",
            "cost_quantization_per_site": 1000,
            "provenance": {
                "producer": "fixture-lookahead",
                "producer_version": "1",
                "grouping_sha256": canonical_sha256(self.refined),
                "placement_sha256": "b" * 64,
                "architecture_sha256": "c" * 64,
            },
            "domains": [{"id": "AB", "fpga_a": "A", "fpga_b": "B"}],
            "bank_pairs": [
                {
                    "id": "bank0",
                    "domain": "AB",
                    "bank_a": {"id": "A0", "x": 0.0, "y": 0.0},
                    "bank_b": {"id": "B0", "x": 100.0, "y": 0.0},
                    "channels": [
                        {
                            "id": f"channel{index}",
                            "order": index,
                            "pin_a": {"x": 0.0, "y": float(10 + 40 * index)},
                            "pin_b": {"x": 100.0, "y": float(10 + 40 * index)},
                        }
                        for index in range(2)
                    ],
                }
            ],
            "groups": [
                {
                    "id": f"group{group_by_signal[signal]}",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": direction,
                    "members": [
                        {
                            "id": signal,
                            "fanout": {"x": source_x, "y": source_y},
                            "fanins": [{"x": sink_x, "y": source_y}],
                        }
                    ],
                }
                for signal, direction, source_x, sink_x, source_y in (
                    ("s0", "a_to_b", 0.0, 100.0, 10.0),
                    ("s1", "b_to_a", 100.0, 0.0, 50.0),
                )
            ],
            "metrics": {
                "groups": 2,
                "signals": 2,
                "fanins": 2,
                "bank_pairs": 1,
                "channels": 2,
            },
        }
        self.bank_report = evaluate_chimew_bank_channel_assignment(
            self.bank_input, executable=self.executables["assigner"]
        )

    def _build(self):
        return build_chimew_phase6_qualification(
            self.schedule,
            self.crossings,
            self.initial,
            self.positions,
            self.refined,
            self.rudy_input,
            self.rudy_report,
            self.bank_input,
            self.bank_report,
        )

    def test_complete_chain_is_sealed_and_replayable(self) -> None:
        certificate = self._build()
        self.assertEqual(certificate["provider"], CHIMEW_QUALIFICATION_PROVIDER)
        self.assertEqual(certificate["status"], "pass")
        self.assertEqual(certificate["metrics"]["artifact_chain_disagreements"], 0)
        validation = validate_chimew_phase6_qualification(
            certificate,
            self.schedule,
            self.crossings,
            self.initial,
            self.positions,
            self.refined,
            self.rudy_input,
            self.rudy_report,
            self.bank_input,
            self.bank_report,
        )
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(validation["signals"], 2)

    def test_cli_materializes_the_same_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = {
                "schedule": self.schedule,
                "crossings": self.crossings,
                "initial": self.initial,
                "positions": self.positions,
                "refined": self.refined,
                "rudy-input": self.rudy_input,
                "rudy-report": self.rudy_report,
                "assignment-input": self.bank_input,
                "assignment-report": self.bank_report,
            }
            paths = {}
            for label, document in documents.items():
                paths[label] = root / f"{label}.json"
                if label == "assignment-input":
                    write_chimew_bank_channel_storage(
                        paths[label], document
                    ).close()
                elif label == "assignment-report":
                    write_chimew_bank_channel_report_storage(
                        paths[label], document
                    ).close()
                elif label == "schedule":
                    write_tdm_schedule_storage(paths[label], document)
                else:
                    write_json(paths[label], document)
            output = root / "qualification.json"
            arguments = ["pin-plan", "chimew-qualify"]
            for option in (
                "schedule",
                "crossings",
                "initial",
                "positions",
                "refined",
                "rudy-input",
                "rudy-report",
                "assignment-input",
                "assignment-report",
            ):
                cli_name = {
                    "initial": "initial-grouping",
                    "refined": "refined-grouping",
                }.get(option, option)
                arguments.extend([f"--{cli_name}", str(paths[option])])
            arguments.extend(["--output", str(output)])
            self.assertEqual(main(arguments), 0)
            self.assertEqual(read_json(output), self._build())

    def test_tampering_and_cross_run_mixing_are_rejected(self) -> None:
        bad_rudy = copy.deepcopy(self.rudy_report)
        bad_rudy["bins"][0]["load"] += 1.0
        with self.assertRaisesRegex(ValidationError, "RUDY bin"):
            build_chimew_phase6_qualification(
                self.schedule,
                self.crossings,
                self.initial,
                self.positions,
                self.refined,
                self.rudy_input,
                bad_rudy,
                self.bank_input,
                self.bank_report,
            )
        bad_bank = copy.deepcopy(self.bank_input)
        bad_bank["provenance"]["grouping_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "provenance chain"):
            build_chimew_phase6_qualification(
                self.schedule,
                self.crossings,
                self.initial,
                self.positions,
                self.refined,
                self.rudy_input,
                self.rudy_report,
                bad_bank,
                evaluate_chimew_bank_channel_assignment(
                    bad_bank, executable=self.executables["assigner"]
                ),
            )

    def test_rejected_rudy_gate_cannot_qualify_phase6(self) -> None:
        rejected_input = copy.deepcopy(self.rudy_input)
        rejected_input["max_utilization"] = 0.01
        rejected_report = evaluate_chimew_rudy(
            rejected_input, executable=self.executables["rudy"]
        )
        with self.assertRaisesRegex(ValidationError, "gate did not pass"):
            build_chimew_phase6_qualification(
                self.schedule,
                self.crossings,
                self.initial,
                self.positions,
                self.refined,
                rejected_input,
                rejected_report,
                self.bank_input,
                self.bank_report,
            )

    def test_cli_runs_the_complete_phase6_pipeline_without_reoptimizing_report(self) -> None:
        platform = {
            "schema": "emuflow.boarddb/v1",
            "platform": {
                "name": self.schedule["platform"],
                "kind": "hardware",
                "description": "Chimew pipeline fixture",
            },
            "fpgas": [
                {
                    "id": fpga,
                    "part": "fixture",
                    "utilization_limit": 1.0,
                    "capacity": {"lut": 100},
                }
                for fpga in ("A", "B")
            ],
            "links": [
                {
                    "id": "AB",
                    "endpoints": ["A", "B"],
                    "direction": "full_duplex",
                    "mode": "parallel",
                    "data_lanes_per_direction": 2,
                    "fabric_clock_mhz": 100.0,
                    "latency_cycles": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_paths = {}
            source_digests = {}
            for label, payload in {
                "routing": b"fixture routing source\n",
                "placement": b"fixture placement source\n",
                "netlist": b"fixture netlist source\n",
                "architecture": b"fixture architecture source\n",
                "package_pins": b"fixture package-pin inventory\n",
            }.items():
                source_paths[label] = root / f"{label}.source"
                source_paths[label].write_bytes(payload)
                source_digests[label] = hashlib.sha256(payload).hexdigest()
            crossings = copy.deepcopy(self.crossings)
            crossings["provenance"]["routing_sha256"] = source_digests[
                "routing"
            ]
            positions = copy.deepcopy(self.positions)
            positions["provenance"]["placement_sha256"] = source_digests[
                "placement"
            ]
            rudy_input = copy.deepcopy(self.rudy_input)
            rudy_input["provenance"].update(
                {
                    "placement_sha256": source_digests["placement"],
                    "netlist_sha256": source_digests["netlist"],
                    "architecture_sha256": source_digests["architecture"],
                }
            )
            initial = build_chimew_initial_groups(
                self.schedule, crossings, executable=self.executables["grouper"]
            )
            refined = refine_chimew_groups(
                self.schedule,
                crossings,
                initial,
                positions,
                executable=self.executables["refiner"],
            )
            bank_input = copy.deepcopy(self.bank_input)
            bank_input["provenance"].update(
                {
                    "grouping_sha256": canonical_sha256(refined),
                    "placement_sha256": source_digests["placement"],
                    "architecture_sha256": source_digests["architecture"],
                }
            )
            paths = {}
            for label, document in {
                "schedule": self.schedule,
                "platform": platform,
                "crossings": crossings,
                "positions": positions,
                "rudy-input": rudy_input,
                "assignment-input": bank_input,
            }.items():
                paths[label] = root / f"{label}.json"
                if label == "assignment-input":
                    write_chimew_bank_channel_storage(
                        paths[label], document
                    ).close()
                elif label == "schedule":
                    write_tdm_schedule_storage(paths[label], document)
                else:
                    write_json(paths[label], document)
            electrical = {
                "schema": CHIMEW_ELECTRICAL_MAP_SCHEMA,
                "provider": CHIMEW_ELECTRICAL_MAP_PROVIDER,
                "design": self.schedule["design"],
                "platform": self.schedule["platform"],
                "provenance": {
                    "producer": "fixture-bsp",
                    "producer_version": "1",
                    "boarddb_sha256": hashlib.sha256(
                        paths["platform"].read_bytes()
                    ).hexdigest(),
                    "package_pin_inventory_sha256": source_digests[
                        "package_pins"
                    ],
                },
                "fpga_y_bounds": [
                    {"fpga": fpga, "y_min": 0.0, "y_max": 100.0}
                    for fpga in ("A", "B")
                ],
                "channels": [
                    {
                        "chimew_channel": f"channel{index}",
                        "link": "AB",
                        "physical_lane": index,
                        "direction": "either",
                        "bank_a": "A0",
                        "bank_b": "B0",
                        "package_pin_a": f"A{index}",
                        "package_pin_b": f"B{index}",
                        "iostandard": "LVCMOS18",
                        "supported_iostandards": ["LVCMOS18"],
                        "bank_voltage": 1.8,
                        "electrical_class": "single_ended_parallel",
                        "reserved": False,
                    }
                    for index in range(2)
                ],
                "metrics": {"channels": 2, "package_pins": 4, "concrete_lanes": 2},
            }
            paths["electrical-map"] = root / "electrical-map.json"
            write_json(paths["electrical-map"], electrical)
            output = root / "pipeline"
            arguments = ["pin-plan", "chimew-run"]
            for option in (
                "schedule",
                "platform",
                "crossings",
                "positions",
                "rudy-input",
                "assignment-input",
                "electrical-map",
            ):
                arguments.extend([f"--{option}", str(paths[option])])
            for option in ("grouper", "refiner", "rudy", "assigner"):
                arguments.extend([f"--{option}", self.executables[option]])
            for label, source in source_paths.items():
                arguments.extend(
                    [f"--{label.replace('_', '-')}-source", str(source)]
                )
            arguments.extend(["--out", str(output)])
            self.assertEqual(main(arguments), 0)
            report = read_json(output / "pipeline_report.json")
            self.assertEqual(
                report["provider"], CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["metrics"]["artifact_chain_disagreements"], 0)
            self.assertTrue(
                (
                    output
                    / report["artifacts"][
                        "adapter_qualification_certificate"
                    ]["path"]
                ).is_file()
            )
            self.assertEqual(main(["pin-plan", "chimew-validate", str(output)]), 0)
            validation = validate_chimew_phase6_pipeline(output)
            self.assertEqual(validation["artifact_hashes_verified"], 31)
            self.assertEqual(
                validation["qualification_scope"], "byte-bound-source-artifacts"
            )

            committed_before = (output / "pipeline_report.json").read_bytes()
            committed_report = read_json(output / "pipeline_report.json")
            committed_generation = (
                output
                / committed_report["artifacts"]["generation_manifest"]["path"]
            ).parent
            adapter_dir = (
                output
                / committed_report["artifacts"]["adapter_report"]["path"]
            ).parent
            committed_files = {
                path.relative_to(committed_generation): path.read_bytes()
                for path in committed_generation.rglob("*")
                if path.is_file()
            }

            # Bare files are not generations, even when their names and bytes
            # look superficially plausible.
            real_pin_store = (
                output
                / committed_report["artifacts"]["adapter_pin_plan_store"]["path"]
            )
            with closing(sqlite3.connect(real_pin_store)) as connection, connection:
                application_id = connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0]
                user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            fake_sqlite_temporary = adapter_dir / "pin_plan.incomplete.sqlite"
            with (
                closing(sqlite3.connect(fake_sqlite_temporary)) as connection,
                connection,
            ):
                connection.execute(f"PRAGMA application_id={application_id}")
                connection.execute(f"PRAGMA user_version={user_version}")
            fake_sqlite_digest = hashlib.sha256(
                fake_sqlite_temporary.read_bytes()
            ).hexdigest()
            fake_sqlite = adapter_dir / f"pin_plan.{fake_sqlite_digest}.sqlite"
            fake_sqlite_temporary.replace(fake_sqlite)
            with self.assertRaisesRegex(ValidationError, "coverage is not exact"):
                validate_chimew_phase6_pipeline(output)
            fake_sqlite.unlink()
            real_bank = read_json(
                output
                / committed_report["artifacts"]["adapter_bank_channel_report"][
                    "path"
                ]
            )
            fake_json_temporary = (
                adapter_dir / "bank_channel_report.incomplete.json"
            )
            write_json(
                fake_json_temporary,
                {
                    "schema": real_bank["schema"],
                    "provider": real_bank["header"]["provider"],
                },
            )
            fake_json_digest = hashlib.sha256(
                fake_json_temporary.read_bytes()
            ).hexdigest()
            fake_json = (
                adapter_dir / f"bank_channel_report.{fake_json_digest}.json"
            )
            fake_json_temporary.replace(fake_json)
            with self.assertRaisesRegex(ValidationError, "coverage is not exact"):
                validate_chimew_phase6_pipeline(output)
            fake_json.unlink()

            # A fully resealed inventory is still not a legal orphan when a
            # logical artifact is malformed.  The writer must not collect it
            # merely because every filename and SHA-256 agrees.
            malformed_staging = root / "malformed-generation"
            shutil.copytree(committed_generation, malformed_staging)
            malformed_manifest_path = (
                malformed_staging / "generation_manifest.json"
            )
            malformed_manifest = read_json(malformed_manifest_path)
            malformed_schedule = (
                malformed_staging
                / malformed_manifest["artifacts"]["schedule"]["path"]
            )
            write_json(malformed_schedule, {})
            malformed_manifest["artifacts"]["schedule"]["sha256"] = (
                hashlib.sha256(malformed_schedule.read_bytes()).hexdigest()
            )
            write_json(malformed_manifest_path, malformed_manifest)
            malformed_digest = hashlib.sha256(
                malformed_manifest_path.read_bytes()
            ).hexdigest()
            malformed_generation = output / "generations" / malformed_digest
            malformed_staging.replace(malformed_generation)
            with self.assertRaisesRegex(ValidationError, "coverage is not exact"):
                validate_chimew_phase6_pipeline(output)
            run_chimew_phase6_pipeline(
                paths["schedule"],
                paths["platform"],
                paths["crossings"],
                paths["positions"],
                paths["rudy-input"],
                paths["assignment-input"],
                paths["electrical-map"],
                output,
                source_paths=source_paths,
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
                rudy=self.executables["rudy"],
                assigner=self.executables["assigner"],
                region_count=31,
            )
            self.assertTrue(malformed_generation.is_dir())
            shutil.rmtree(malformed_generation)
            self.assertEqual(
                validate_chimew_phase6_pipeline(output)["status"], "pass"
            )

            import emuflow.transport.chimew.chimew_pipeline as chimew_pipeline_module

            for identity_field, wrong_value in (
                ("design", "wrong-design"),
                ("platform", "wrong-platform"),
            ):
                identity_staging = root / f"mismatched-{identity_field}"
                shutil.copytree(committed_generation, identity_staging)
                identity_manifest_path = (
                    identity_staging / "generation_manifest.json"
                )
                identity_manifest = read_json(identity_manifest_path)
                identity_manifest[identity_field] = wrong_value
                write_json(identity_manifest_path, identity_manifest)
                identity_digest = hashlib.sha256(
                    identity_manifest_path.read_bytes()
                ).hexdigest()
                identity_generation = output / "generations" / identity_digest
                identity_staging.replace(identity_generation)
                with self.subTest(identity_field=identity_field):
                    with self.assertRaisesRegex(
                        ValidationError, "generation identity differs"
                    ):
                        chimew_pipeline_module._validate_pipeline_generation_directory(
                            identity_generation
                        )
                    with self.assertRaisesRegex(
                        ValidationError, "coverage is not exact"
                    ):
                        validate_chimew_phase6_pipeline(output)
                shutil.rmtree(identity_generation)
            self.assertEqual(
                validate_chimew_phase6_pipeline(output)["status"], "pass"
            )

            # Change two real inputs, then fail exactly at the outer commit.
            # Every file named by the preceding report must remain byte-identical.
            changed_platform = read_json(paths["platform"])
            changed_platform["platform"]["description"] = "changed generation"
            write_json(paths["platform"], changed_platform)
            changed_electrical = read_json(paths["electrical-map"])
            changed_electrical["provenance"]["boarddb_sha256"] = hashlib.sha256(
                paths["platform"].read_bytes()
            ).hexdigest()
            write_json(paths["electrical-map"], changed_electrical)

            real_pipeline_write_json = chimew_pipeline_module.write_json

            def fail_outer_commit(path, value, **kwargs):
                if Path(path).name == "pipeline_report.json":
                    raise OSError("injected outer pipeline commit failure")
                return real_pipeline_write_json(path, value, **kwargs)

            with mock.patch(
                "emuflow.transport.chimew.chimew_pipeline.write_json",
                side_effect=fail_outer_commit,
            ):
                with self.assertRaisesRegex(OSError, "outer pipeline commit"):
                    run_chimew_phase6_pipeline(
                        paths["schedule"],
                        paths["platform"],
                        paths["crossings"],
                        paths["positions"],
                        paths["rudy-input"],
                        paths["assignment-input"],
                        paths["electrical-map"],
                        output,
                        source_paths=source_paths,
                        grouper=self.executables["grouper"],
                        refiner=self.executables["refiner"],
                        rudy=self.executables["rudy"],
                        assigner=self.executables["assigner"],
                        region_count=7,
                    )
            self.assertEqual(
                (output / "pipeline_report.json").read_bytes(), committed_before
            )
            self.assertEqual(
                {
                    path.relative_to(committed_generation): path.read_bytes()
                    for path in committed_generation.rglob("*")
                    if path.is_file()
                },
                committed_files,
            )
            self.assertEqual(
                validate_chimew_phase6_pipeline(output)["status"], "pass"
            )
            generations_after_failure = {
                path.name
                for path in (output / "generations").iterdir()
                if path.is_dir()
            }
            self.assertGreater(len(generations_after_failure), 1)

            run_chimew_phase6_pipeline(
                paths["schedule"],
                paths["platform"],
                paths["crossings"],
                paths["positions"],
                paths["rudy-input"],
                paths["assignment-input"],
                paths["electrical-map"],
                output,
                source_paths=source_paths,
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
                rudy=self.executables["rudy"],
                assigner=self.executables["assigner"],
                region_count=7,
            )
            refreshed_report = read_json(output / "pipeline_report.json")
            refreshed_generation = (
                output
                / refreshed_report["artifacts"]["generation_manifest"]["path"]
            ).parent
            self.assertEqual(
                {
                    path.resolve()
                    for path in (output / "generations").iterdir()
                    if path.is_dir()
                },
                {refreshed_generation.resolve()},
            )
            self.assertEqual(
                validate_chimew_phase6_pipeline(output)["status"], "pass"
            )
            report = refreshed_report

            # A shared reader lease keeps its report generation alive while a
            # later writer waits for the exclusive lock.  Two writers also
            # enter the underlying producer one at a time.
            reader_entered = threading.Event()
            release_reader = threading.Event()
            reader_errors = []
            leased_generation = refreshed_generation

            def hold_reader():
                try:
                    with read_chimew_phase6_pipeline(output) as (
                        leased_report,
                        leased_validation,
                    ):
                        self.assertEqual(leased_validation["status"], "pass")
                        self.assertEqual(leased_report, refreshed_report)
                        reader_entered.set()
                        self.assertTrue(release_reader.wait(10.0))
                        self.assertTrue(leased_generation.is_dir())
                except BaseException as error:
                    reader_errors.append(error)

            writer_errors = []

            def run_locked_writer(region_count):
                try:
                    run_chimew_phase6_pipeline(
                        paths["schedule"],
                        paths["platform"],
                        paths["crossings"],
                        paths["positions"],
                        paths["rudy-input"],
                        paths["assignment-input"],
                        paths["electrical-map"],
                        output,
                        source_paths=source_paths,
                        grouper=self.executables["grouper"],
                        refiner=self.executables["refiner"],
                        rudy=self.executables["rudy"],
                        assigner=self.executables["assigner"],
                        region_count=region_count,
                    )
                except BaseException as error:
                    writer_errors.append(error)

            reader_thread = threading.Thread(target=hold_reader)
            writer_thread = threading.Thread(
                target=run_locked_writer, args=(8,)
            )
            reader_thread.start()
            self.assertTrue(reader_entered.wait(10.0))
            writer_thread.start()
            writer_thread.join(0.2)
            self.assertTrue(writer_thread.is_alive())
            self.assertTrue(leased_generation.is_dir())
            release_reader.set()
            reader_thread.join(10.0)
            writer_thread.join(30.0)
            self.assertFalse(reader_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual(reader_errors, [])
            self.assertEqual(writer_errors, [])
            self.assertFalse(leased_generation.exists())

            import emuflow.transport.chimew.chimew_pipeline as chimew_pipeline_module

            real_locked_writer = (
                chimew_pipeline_module._run_chimew_phase6_pipeline_locked
            )
            producer_entered = threading.Event()
            release_first_writer = threading.Event()
            producer_guard = threading.Lock()
            active_producers = 0
            maximum_active_producers = 0
            producer_calls = 0

            def observe_locked_writer(*args, **kwargs):
                nonlocal active_producers, maximum_active_producers, producer_calls
                with producer_guard:
                    active_producers += 1
                    producer_calls += 1
                    call_number = producer_calls
                    maximum_active_producers = max(
                        maximum_active_producers, active_producers
                    )
                try:
                    if call_number == 1:
                        producer_entered.set()
                        self.assertTrue(release_first_writer.wait(10.0))
                    return real_locked_writer(*args, **kwargs)
                finally:
                    with producer_guard:
                        active_producers -= 1

            writer_errors.clear()
            with mock.patch(
                "emuflow.transport.chimew.chimew_pipeline._run_chimew_phase6_pipeline_locked",
                side_effect=observe_locked_writer,
            ):
                first_writer = threading.Thread(
                    target=run_locked_writer, args=(9,)
                )
                second_writer = threading.Thread(
                    target=run_locked_writer, args=(10,)
                )
                first_writer.start()
                self.assertTrue(producer_entered.wait(10.0))
                second_writer.start()
                second_writer.join(0.2)
                self.assertTrue(second_writer.is_alive())
                self.assertEqual(maximum_active_producers, 1)
                release_first_writer.set()
                first_writer.join(30.0)
                second_writer.join(30.0)
            self.assertFalse(first_writer.is_alive())
            self.assertFalse(second_writer.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertEqual(maximum_active_producers, 1)
            self.assertEqual(producer_calls, 2)
            self.assertEqual(
                validate_chimew_phase6_pipeline(output)["status"], "pass"
            )
            self.assertEqual(
                len(
                    [
                        path
                        for path in (output / "generations").iterdir()
                        if path.is_dir()
                    ]
                ),
                1,
            )
            report = read_json(output / "pipeline_report.json")
            bound_plan = read_pin_plan(
                output / report["artifacts"]["adapter_pin_plan"]["path"],
                materialize=True,
            )
            bound_binding = read_chimew_binding(
                output
                / report["artifacts"]["adapter_electrical_binding"]["path"],
                materialize=True,
            )
            self.assertEqual(
                bound_plan["configuration"]["qualification_scope"],
                "byte-bound-source-artifacts",
            )
            self.assertEqual(
                bound_binding["provenance"]["qualification_scope"],
                "byte-bound-source-artifacts",
            )
            mixed_embedded_binding = copy.deepcopy(bound_binding)
            mixed_embedded_plan = copy.deepcopy(bound_plan)
            mixed_embedded_certificate = mixed_embedded_binding[
                "lookahead_qualification"
            ]
            mixed_embedded_certificate["source_binding"]["digests"][
                "package_pins"
            ] = "0" * 64
            mixed_embedded_certificate["source_binding_sha256"] = canonical_sha256(
                mixed_embedded_certificate["source_binding"]
            )
            mixed_embedded_certificate.pop("qualification_sha256")
            mixed_embedded_certificate["qualification_sha256"] = canonical_sha256(
                mixed_embedded_certificate
            )
            mixed_embedded_plan["configuration"]["qualification_sha256"] = (
                mixed_embedded_certificate["qualification_sha256"]
            )
            mixed_embedded_binding["provenance"]["qualification_sha256"] = (
                mixed_embedded_certificate["qualification_sha256"]
            )
            with self.assertRaisesRegex(
                ValidationError, "package-pin provenance does not agree"
            ):
                validate_chimew_phase6_binding(
                    self.schedule,
                    Platform.from_dict(platform),
                    mixed_embedded_plan,
                    mixed_embedded_binding,
                )
            qualification_document = read_json(
                output / report["artifacts"]["qualification"]["path"]
            )
            inconsistent_source = copy.deepcopy(qualification_document)
            inconsistent_source["source_binding"]["digests"]["routing"] = (
                "0" * 64
            )
            inconsistent_source["source_binding_sha256"] = canonical_sha256(
                inconsistent_source["source_binding"]
            )
            inconsistent_source.pop("qualification_sha256")
            inconsistent_source["qualification_sha256"] = canonical_sha256(
                inconsistent_source
            )
            with self.assertRaisesRegex(
                ValidationError, "source provenance is inconsistent"
            ):
                validate_chimew_qualification_seal(
                    inconsistent_source, self.schedule
                )

            mixed_package_pins = copy.deepcopy(qualification_document)
            mixed_package_pins["source_binding"]["digests"]["package_pins"] = (
                "0" * 64
            )
            mixed_package_pins["source_binding_sha256"] = canonical_sha256(
                mixed_package_pins["source_binding"]
            )
            mixed_package_pins.pop("qualification_sha256")
            mixed_package_pins["qualification_sha256"] = canonical_sha256(
                mixed_package_pins
            )
            with self.assertRaisesRegex(
                ValidationError, "package-pin provenance does not agree"
            ):
                build_chimew_phase6_pin_plan(
                    self.schedule,
                    Platform.from_dict(platform),
                    bank_input,
                    electrical,
                    qualification_document=mixed_package_pins,
                    bank_channel_report_document=read_chimew_bank_channel_report(
                        output
                        / report["artifacts"]["bank_channel_report"]["path"]
                    ),
                    executable=self.executables["assigner"],
                )

            direct_lane = copy.deepcopy(self.schedule)
            for entry in direct_lane["entries"]:
                entry.pop("tdm_ratio")
            write_tdm_schedule_storage(paths["schedule"], direct_lane)
            direct_output = root / "direct-lane-pipeline"
            direct_arguments = list(arguments[:-2])
            direct_arguments.extend(["--out", str(direct_output)])
            self.assertEqual(main(direct_arguments), 0)
            direct_report = read_json(direct_output / "pipeline_report.json")
            self.assertEqual(direct_report["status"], "pass")
            self.assertEqual(
                validate_chimew_phase6_pipeline(direct_output)["status"], "pass"
            )
            downgraded_output = root / "downgraded-pipeline"
            shutil.copytree(direct_output, downgraded_output)
            downgraded_report = read_json(
                downgraded_output / "pipeline_report.json"
            )
            downgraded_report["provider"] = CHIMEW_PIPELINE_PROVIDER
            downgraded_report["qualification_scope"] = (
                "declared-digest-artifact-chain"
            )
            downgraded_source_root = (
                downgraded_output
                / next(
                    record["path"]
                    for label, record in downgraded_report["artifacts"].items()
                    if label.startswith("source_")
                )
            ).parent
            for label in tuple(downgraded_report["artifacts"]):
                if label.startswith("source_"):
                    downgraded_report["artifacts"].pop(label)
            shutil.rmtree(downgraded_source_root)
            write_json(
                downgraded_output / "pipeline_report.json", downgraded_report
            )
            with self.assertRaises(ValidationError):
                validate_chimew_phase6_pipeline(downgraded_output)

            legacy_output = root / "legacy-pipeline"
            legacy_arguments = ["pin-plan", "chimew-run"]
            for option in (
                "schedule",
                "platform",
                "crossings",
                "positions",
                "rudy-input",
                "assignment-input",
                "electrical-map",
            ):
                legacy_arguments.extend([f"--{option}", str(paths[option])])
            for option in ("grouper", "refiner", "rudy", "assigner"):
                legacy_arguments.extend([f"--{option}", self.executables[option]])
            legacy_arguments.extend(["--out", str(legacy_output)])
            self.assertEqual(main(legacy_arguments), 0)
            legacy_report = read_json(legacy_output / "pipeline_report.json")
            self.assertEqual(
                validate_chimew_phase6_pipeline(legacy_output)[
                    "qualification_scope"
                ],
                "declared-digest-artifact-chain",
            )
            legacy_generation = (
                legacy_output
                / legacy_report["artifacts"]["generation_manifest"]["path"]
            ).parent
            for child in legacy_generation.iterdir():
                if child.name != "generation_manifest.json":
                    shutil.move(str(child), legacy_output / child.name)
            shutil.rmtree(legacy_output / "generations")
            legacy_report["artifacts"].pop("generation_manifest")
            for record in legacy_report["artifacts"].values():
                relative = Path(record["path"])
                record["path"] = Path(*relative.parts[2:]).as_posix()
            legacy_report.pop("qualification_scope")
            legacy_pin_path = (
                legacy_output
                / legacy_report["artifacts"]["adapter_pin_plan"]["path"]
            )
            legacy_binding_path = (
                legacy_output
                / legacy_report["artifacts"]["adapter_electrical_binding"]["path"]
            )
            legacy_adapter_path = (
                legacy_output
                / legacy_report["artifacts"]["adapter_report"]["path"]
            )
            legacy_pin_selector = read_json(legacy_pin_path)
            legacy_pin_store = legacy_pin_path.with_name(
                legacy_pin_selector["artifact"]
            )
            legacy_pin = read_pin_plan(legacy_pin_path, materialize=True)
            legacy_position_path = (
                legacy_output
                / legacy_report["artifacts"]["adapter_position_hints"]["path"]
            )
            legacy_position_selector = read_json(legacy_position_path)
            legacy_position_store = legacy_position_path.with_name(
                legacy_position_selector["artifact"]
            )
            legacy_position = read_position_hints(
                legacy_position_path, materialize=True
            )
            legacy_binding_selector = read_json(legacy_binding_path)
            legacy_binding_store = legacy_binding_path.with_name(
                legacy_binding_selector["artifact"]
            )
            legacy_binding = read_chimew_binding(
                legacy_binding_path, materialize=True
            )
            legacy_adapter = read_json(legacy_adapter_path)
            legacy_adapter["schema"] = (
                "emuflow.chimew-phase6-adapter-report/v1"
            )
            legacy_pin["configuration"].pop("qualification_scope")
            legacy_binding["provenance"].pop("qualification_scope")
            legacy_adapter["qualification_validation"].pop(
                "qualification_scope"
            )
            legacy_adapter["artifacts"].pop("electrical_binding_store")
            legacy_adapter["artifacts"].pop("pin_plan_store")
            legacy_adapter["artifacts"].pop("position_hints_store")
            legacy_report["artifacts"].pop(
                "adapter_electrical_binding_store"
            )
            legacy_report["artifacts"].pop("adapter_pin_plan_store")
            legacy_report["artifacts"].pop("adapter_position_hints_store")
            legacy_binding_store.unlink()
            legacy_pin_store.unlink()
            legacy_position_store.unlink()
            write_json(legacy_pin_path, legacy_pin)
            write_json(legacy_position_path, legacy_position)
            write_json(legacy_binding_path, legacy_binding)
            write_json(legacy_adapter_path, legacy_adapter)
            for label, path in {
                "adapter_pin_plan": legacy_pin_path,
                "adapter_position_hints": legacy_position_path,
                "adapter_electrical_binding": legacy_binding_path,
                "adapter_report": legacy_adapter_path,
            }.items():
                legacy_report["artifacts"][label]["sha256"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            write_json(legacy_output / "pipeline_report.json", legacy_report)
            # Inline historical handoffs no longer have a production reader.
            with self.assertRaises(ValidationError):
                validate_chimew_phase6_pipeline(legacy_output)
            (legacy_output / "unexpected.txt").write_text("unexpected\n")
            with self.assertRaisesRegex(ValidationError, "coverage is not exact"):
                validate_chimew_phase6_pipeline(legacy_output)
            (legacy_output / "unexpected.txt").unlink()
            legacy_report["artifacts"]["schedule"]["path"] = "../schedule.json"
            write_json(legacy_output / "pipeline_report.json", legacy_report)
            with self.assertRaisesRegex(ValidationError, "path is unsafe"):
                validate_chimew_phase6_pipeline(legacy_output)

            adapter_tamper = root / "adapter-tamper"
            shutil.copytree(output, adapter_tamper)
            adapter_path = (
                adapter_tamper
                / read_json(adapter_tamper / "pipeline_report.json")["artifacts"][
                    "adapter_report"
                ]["path"]
            )
            adapter_document = read_json(adapter_path)
            adapter_document["provider"] = "tampered-provider"
            write_json(adapter_path, adapter_document)
            tamper_report = read_json(adapter_tamper / "pipeline_report.json")
            tamper_report["artifacts"]["adapter_report"]["sha256"] = hashlib.sha256(
                adapter_path.read_bytes()
            ).hexdigest()
            write_json(adapter_tamper / "pipeline_report.json", tamper_report)
            with self.assertRaisesRegex(
                ValidationError, "generation artifact seal|adapter report"
            ):
                validate_chimew_phase6_pipeline(adapter_tamper)

            (
                output / report["artifacts"]["source_routing"]["path"]
            ).write_bytes(b"tampered\n")
            with self.assertRaisesRegex(
                ValidationError, "generation artifact seal differs|hash differs"
            ):
                validate_chimew_phase6_pipeline(output)


if __name__ == "__main__":
    unittest.main()
