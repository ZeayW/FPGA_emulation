import copy
import io
import itertools
import math
import os
import random
import sqlite3
import subprocess
import tempfile
import unittest
from array import array
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.emuir_compact import write_compact_emuir
from emuflow.ir import EmuIR
from emuflow.partition_ir import load_partition_ir
from emuflow.partition import (
    CUT_MODE_SEQUENTIAL_ONLY,
    CUT_MODE_STATIC_EXACT,
    build_clusters,
    build_partition_assignment,
    compute_cut_nets,
    normalize_partition_constraints,
)
from emuflow.combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
)
from emuflow.partition_pressure import (
    _PackedPressureClusterSequence,
    _PackedPressureNetSequence,
    _PackedPressurePathSequence,
    _PackedStringOrdinalIndex,
    _NativeBinaryPipeWriter,
    _canonical_digest,
    _flow_refinement_configuration,
    _parse_patron_native_output,
    _write_patron_native_input,
    build_partition_pressure_model,
    evaluate_partition_pressure,
    refine_partition_pressure_exhaustive,
    run_partition_pressure_reference,
    run_partition_pressure_native,
    validate_partition_pressure_model,
    validate_partition_pressure_native_bundle,
    validate_partition_pressure_trace,
    validate_partition_pressure_native_against_exhaustive,
    validate_partition_pressure_scalable_trace,
)
from emuflow.phase3 import promote_patron_baseline, run_phase3
from emuflow.phase3 import (
    PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
    _rebase_patron_initial_assignment,
    _select_patron_timing_assignment,
)
from emuflow.phase3_storage import pack_phase3_assignment
from emuflow.platform import Platform
from emuflow.io import read_json, write_json
from emuflow.routing import normalize_route_constraints
from emuflow.opensta import OPENSTA_PROVIDER
from emuflow.sta import (
    STA_PATH_DATABASE_TSV_HEADER,
    import_sta_path_database_tsv_streaming,
)
from tests.test_phase5 import _link, _platform_value
from tests.native_build import patron_refiner


def _endpoint(instance: str, port: str) -> dict:
    return {"instance": instance, "port": port, "bit": 0}


def _ir() -> EmuIR:
    instances = [
        {
            "id": f"u{index}",
            "type": "LUT1",
            "resources": {"lut": 1},
        }
        for index in range(4)
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure",
                "top": "pressure",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": instances,
            "nets": [
                {
                    "id": "critical",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u0", "O")],
                    "sinks": [_endpoint("u1", "I")],
                },
                {
                    "id": "relaxed",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u2", "O")],
                    "sinks": [_endpoint("u3", "I")],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
    )


def _chain_ir(instances: int) -> EmuIR:
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure_chain",
                "top": "pressure_chain",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": [
                {
                    "id": f"u{index:05d}",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                }
                for index in range(instances)
            ],
            "nets": [
                {
                    "id": f"n{index:05d}",
                    "cut_class": "register_output",
                    "drivers": [_endpoint(f"u{index:05d}", "O")],
                    "sinks": [_endpoint(f"u{index + 1:05d}", "I")],
                }
                for index in range(instances - 1)
            ],
            "clocks": [],
            "warnings": [],
        }
    )


def _capacity_release_ir(instances: int = 300) -> EmuIR:
    """Large sparse graph whose second improving move needs another sweep."""

    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "pressure_capacity_release",
                "top": "pressure_capacity_release",
                "source_format": "fixture",
            },
            "ports": [],
            "instances": [
                {
                    "id": f"u{index:05d}",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                }
                for index in range(instances)
            ],
            "nets": [
                {
                    "id": "n00000",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u00000", "O")],
                    "sinks": [_endpoint("u00001", "I")],
                },
                {
                    "id": "n00002",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("u00002", "O")],
                    "sinks": [_endpoint("u00003", "I")],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
    )


class PartitionPressureTest(unittest.TestCase):
    def test_undriven_wire_is_not_materialized_as_transport(self) -> None:
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "undriven_transport",
                    "top": "undriven_transport",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {
                        "id": "sink",
                        "type": "LUT1",
                        "resources": {"lut": 1},
                    }
                ],
                "nets": [
                    {
                        "id": "open_wire",
                        "cut_class": "undriven",
                        "drivers": [],
                        "sinks": [_endpoint("sink", "I")],
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )

        cuts, metrics = compute_cut_nets(ir, {"sink": "fpga2"})

        self.assertEqual(cuts, [])
        self.assertEqual(metrics["cut_nets"], 0)

    def setUp(self) -> None:
        self.ir = _ir()
        self.platform = Platform.from_dict(
            _platform_value(
                "pressure_platform",
                ["a", "b"],
                [_link("ab", "a", "b", lanes=1, latency=1)],
            )
        )
        self.constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        self.clusters = build_clusters(self.ir, self.constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in self.clusters["clusters"]
        }
        self.initial = build_partition_assignment(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "a",
                by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=7,
        )
        self.route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            self.platform,
        )
        self.timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 20.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u0", "O"),
                    "endpoint": _endpoint("u1", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["critical"],
                    "normalized_slack": 0.075,
                },
                {
                    "id": "p-relaxed",
                    "startpoint": _endpoint("u2", "O"),
                    "endpoint": _endpoint("u3", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "slack_ns": 20.0,
                    "fixed_delay_ns": 0.0,
                    "path_nets": ["relaxed"],
                    "normalized_slack": 1.0,
                },
            ],
        }
        self.model = build_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
        )

    def test_compact_pressure_model_matches_reference_and_native_bytes(
        self,
    ) -> None:
        expected = build_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            independent_validation=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with (
                patch(
                    "emuflow.partition_ir._CompactPartitionInstances.__iter__",
                    side_effect=AssertionError("cell dictionaries were decoded"),
                ),
                patch(
                    "emuflow.partition_ir._CompactPartitionNets.__iter__",
                    side_effect=AssertionError("net dictionaries were decoded"),
                ),
            ):
                actual = build_partition_pressure_model(
                    load_partition_ir(ir_path),
                    self.platform,
                    self.clusters,
                    self.constraints,
                    self.timing,
                    self.route_constraints,
                    independent_validation=False,
                )
            self.assertIsInstance(
                actual["nets"], _PackedPressureNetSequence
            )
            self.assertIsInstance(
                actual["clusters"], _PackedPressureClusterSequence
            )
            self.assertIsInstance(
                actual["paths"], _PackedPressurePathSequence
            )
            self.assertEqual(actual, expected)
            reference_input = root / "reference.patron"
            compact_input = root / "compact.patron"
            _write_patron_native_input(
                reference_input,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                expected,
                self.initial,
                0,
                6,
            )
            indexes = _write_patron_native_input(
                compact_input,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                actual,
                self.initial,
                0,
                6,
            )
            self.assertEqual(
                compact_input.read_bytes(), reference_input.read_bytes()
            )
            self.assertEqual(list(indexes["clusters"]), sorted(
                cluster["id"] for cluster in self.clusters["clusters"]
            ))

    def test_production_native_assignment_stays_packed_through_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            compact_ir = load_partition_ir(ir_path)
            compact_clusters = build_clusters(compact_ir, self.constraints)
            compact_initial = build_partition_assignment(
                compact_ir,
                self.platform,
                compact_clusters,
                self.constraints,
                self.initial["cluster_assignment"],
                provider="fixture",
                seed=7,
                _stream_semantic_records=True,
            )
            compact_model = build_partition_pressure_model(
                compact_ir,
                self.platform,
                compact_clusters,
                self.constraints,
                self.timing,
                self.route_constraints,
                independent_validation=False,
            )
            final, trace = run_partition_pressure_native(
                compact_ir,
                self.platform,
                compact_clusters,
                self.constraints,
                self.route_constraints,
                compact_model,
                compact_initial,
                executable=str(patron_refiner()),
                max_moves=0,
                output_validation="online",
                retain_trace_seals=False,
                stream_semantic_records=True,
            )

        cluster_assignment = final["cluster_assignment"]
        instance_assignment = final["instance_assignment"]
        self.assertNotIsInstance(cluster_assignment, dict)
        self.assertEqual(
            cluster_assignment.packed_payload_bytes,
            4 * len(compact_clusters["clusters"]),
        )
        self.assertEqual(
            dict(cluster_assignment.items()),
            self.initial["cluster_assignment"],
        )
        self.assertNotIsInstance(instance_assignment, dict)
        self.assertTrue(callable(instance_assignment.cluster_part))
        self.assertTrue(
            all("clusters" not in record for record in final["partitions"])
        )
        self.assertEqual(
            sum(record["cluster_count"] for record in final["partitions"]),
            len(compact_clusters["clusters"]),
        )
        packed = pack_phase3_assignment(final, compact_clusters)
        self.assertEqual(
            packed["cluster_count"], len(compact_clusters["clusters"])
        )
        self.assertEqual(trace["trace_storage"], "summary-only")
        self.assertEqual(
            trace["native_input_transport"], "typed-binary-pipe"
        )
        self.assertGreater(trace["native_input_bytes"], 0)
        self.assertGreater(trace["native_input_records"], 0)
        self.assertIsInstance(trace["native_input_crc32"], int)
        self.assertNotIn("moves", trace)

    def test_native_patron_explicit_input_copy_preserves_debug_protocol(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy_path = Path(temporary) / "debug" / "patron.in"
            with patch.dict(
                os.environ,
                {"EMUFLOW_PATRON_INPUT_COPY": str(copy_path)},
            ):
                final, trace = run_partition_pressure_native(
                    self.ir,
                    self.platform,
                    self.clusters,
                    self.constraints,
                    self.route_constraints,
                    self.model,
                    self.initial,
                    executable=str(patron_refiner()),
                    max_moves=0,
                )

            self.assertEqual(
                final["cluster_assignment"],
                self.initial["cluster_assignment"],
            )
            self.assertEqual(
                trace["native_input_transport"], "atomic-text-file"
            )
            self.assertIsNone(trace["native_input_crc32"])
            self.assertEqual(
                copy_path.stat().st_size, trace["native_input_bytes"]
            )
            self.assertGreater(trace["native_input_records"], 0)
            self.assertEqual(
                copy_path.read_text().splitlines()[0],
                "EMUFLOW_PATRON_INPUT_V6_BANKS_V1",
            )

    def test_native_patron_typed_input_rejects_corruption_and_framing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            typed_path = root / "patron.bin"
            stream = typed_path.open("wb")
            indexes = _write_patron_native_input(
                None,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                self.model,
                self.initial,
                0,
                6,
                _writer=_NativeBinaryPipeWriter(stream),
            )
            self.assertTrue(stream.closed)
            self.assertEqual(
                indexes["input"]["transport"], "typed-binary-pipe"
            )
            valid = typed_path.read_bytes()
            variants = {
                "corrupted": (
                    valid[: len(valid) // 2]
                    + bytes([valid[len(valid) // 2] ^ 1])
                    + valid[len(valid) // 2 + 1 :]
                ),
                "truncated": valid[:-1],
                "trailing": valid + b"x",
            }
            for label, payload in variants.items():
                with self.subTest(label=label):
                    candidate = root / f"{label}.bin"
                    output = root / f"{label}.out"
                    candidate.write_bytes(payload)
                    completed = subprocess.run(
                        [
                            str(patron_refiner()),
                            "--typed-input",
                            str(candidate),
                            str(output),
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("PATRON typed input", completed.stderr)
                    self.assertFalse(output.exists())

    def test_compact_pressure_model_randomized_differential(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for seed in range(24):
                generator = random.Random(seed)
                instance_count = 12
                value = {
                    "schema": "emuflow.emuir/v1",
                    "design": {
                        "name": f"pressure_random_{seed}",
                        "top": "pressure_random",
                        "source_format": "fixture",
                    },
                    "ports": [],
                    "instances": [
                        {
                            "id": f"u{index:02d}",
                            "type": "LUT1",
                            "resources": {"lut": 1},
                        }
                        for index in range(instance_count)
                    ],
                    "nets": [],
                    "clocks": [],
                    "warnings": [],
                }
                for index in range(24):
                    drivers = generator.sample(
                        range(instance_count), generator.choice((1, 1, 2))
                    )
                    sinks = generator.sample(
                        range(instance_count), generator.randrange(1, 7)
                    )
                    value["nets"].append(
                        {
                            "id": f"n{index:02d}",
                            "cut_class": generator.choice(
                                ("register_output", "register_input", "clock")
                            ),
                            "drivers": [
                                _endpoint(f"u{owner:02d}", "O")
                                for owner in drivers
                            ],
                            "sinks": [
                                _endpoint(f"u{owner:02d}", "I")
                                for owner in sinks
                            ],
                        }
                    )
                reference_ir = EmuIR(value)
                constraints = normalize_partition_constraints(
                    {
                        "schema": "emuflow.partition-constraints/v1",
                        "min_used_fpgas": 2,
                        "balance_tolerance": 1.0,
                    },
                    reference_ir,
                    self.platform,
                )
                clusters = build_clusters(reference_ir, constraints)
                timing = {
                    **self.timing,
                    "design": value["design"]["name"],
                    "paths": [
                        {
                            "id": f"p{index:02d}",
                            "startpoint": _endpoint(
                                f"u{generator.randrange(instance_count):02d}",
                                "O",
                            ),
                            "endpoint": _endpoint(
                                f"u{generator.randrange(instance_count):02d}",
                                "I",
                            ),
                            "clock_domain": "clk",
                            "clock_period_ns": 10.0,
                            "slack_ns": float(generator.randrange(-2, 6)),
                            "path_nets": [
                                f"n{net:02d}"
                                for net in generator.sample(
                                    range(24), generator.randrange(1, 6)
                                )
                            ],
                        }
                        for index in range(10)
                    ],
                }
                expected = build_partition_pressure_model(
                    reference_ir,
                    self.platform,
                    clusters,
                    constraints,
                    timing,
                    self.route_constraints,
                    independent_validation=False,
                )
                path = root / f"random-{seed}.emuir.sqlite"
                write_compact_emuir(path, value)
                actual = build_partition_pressure_model(
                    load_partition_ir(path),
                    self.platform,
                    clusters,
                    constraints,
                    timing,
                    self.route_constraints,
                    independent_validation=False,
                )
                self.assertEqual(actual, expected, f"seed={seed}")

    def test_compact_pressure_model_rejects_corrupt_relations(self) -> None:
        mutations = [
            ("logical-schema", "logical schema is invalid", lambda connection: connection.execute(
                "UPDATE metadata SET value='emuflow.emuir/v999' "
                "WHERE key='logical_schema'"
            )),
            ("invalid-cut-class", "net cut class is invalid", lambda connection: connection.execute(
                "UPDATE net_records SET cut_class='not-a-cut-class' WHERE seq=0"
            )),
            ("invalid-role", "endpoint role is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET role=2 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("negative-ordinal", "endpoint sequence is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET ordinal=-1 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("orphan-net", "orphaned endpoints", lambda connection: connection.execute(
                "UPDATE endpoint_records SET net_seq=999 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("missing-port", "endpoint port is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET port_seq=999 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("missing-owner", "endpoint owner is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET instance_seq=999 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("wrong-top-level-owner", "top-level endpoint is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET instance_seq=NULL "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("negative-bit", "endpoint bit is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET bit=-1 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("real-bit", "endpoint bit is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET bit=0.5 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("text-bit", "endpoint bit is invalid", lambda connection: connection.execute(
                "UPDATE endpoint_records SET bit='zero' "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
            ("net-gap", "net sequence is invalid", lambda connection: (
                connection.execute(
                    "UPDATE endpoint_records SET net_seq=9 WHERE net_seq=1"
                ),
                connection.execute(
                    "UPDATE net_records SET seq=9 WHERE seq=1"
                ),
            )),
            ("missing-endpoint", "record counts disagree", lambda connection: connection.execute(
                "DELETE FROM endpoint_records "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            )),
        ]
        for label, message, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "design.emuir.sqlite"
                write_compact_emuir(path, self.ir.to_dict())
                with sqlite3.connect(path) as connection:
                    mutate(connection)
                with self.assertRaisesRegex(ValidationError, message):
                    build_partition_pressure_model(
                        load_partition_ir(path),
                        self.platform,
                        self.clusters,
                        self.constraints,
                        self.timing,
                        self.route_constraints,
                        independent_validation=False,
                    )

    def test_compact_pressure_model_accepts_sparse_endpoint_ordinals(self) -> None:
        expected = build_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            independent_validation=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, self.ir.to_dict())
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE endpoint_records SET ordinal=ordinal+1000000"
                )
            actual = build_partition_pressure_model(
                load_partition_ir(path),
                self.platform,
                self.clusters,
                self.constraints,
                self.timing,
                self.route_constraints,
                independent_validation=False,
            )
        self.assertEqual(actual, expected)

    def test_native_input_publish_is_atomic_on_validation_and_io_faults(
        self,
    ) -> None:
        parent_fsync_calls = 0

        def fail_parent_fsync(_descriptor: int) -> None:
            nonlocal parent_fsync_calls
            parent_fsync_calls += 1
            if parent_fsync_calls == 2:
                raise OSError("synthetic parent fsync fault")

        fault_patches = (
            (
                "validation",
                patch(
                    "emuflow.partition_pressure._NativeLineWriter.append",
                    side_effect=ValidationError("synthetic validation fault"),
                ),
                ValidationError,
            ),
            (
                "file-fsync",
                patch(
                    "emuflow.partition_pressure.os.fsync",
                    side_effect=OSError("synthetic fsync fault"),
                ),
                OSError,
            ),
            (
                "parent-fsync",
                patch(
                    "emuflow.partition_pressure.os.fsync",
                    side_effect=fail_parent_fsync,
                ),
                OSError,
            ),
            (
                "replace",
                patch(
                    "emuflow.partition_pressure.os.replace",
                    side_effect=OSError("synthetic replace fault"),
                ),
                OSError,
            ),
        )
        for label, fault, exception in fault_patches:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "patron.in"
                target.write_bytes(b"existing-published-input\n")
                with fault, self.assertRaises(exception):
                    _write_patron_native_input(
                        target,
                        self.platform,
                        self.clusters,
                        self.constraints,
                        self.route_constraints,
                        self.model,
                        self.initial,
                        0,
                        6,
                    )
                self.assertEqual(
                    target.read_bytes(), b"existing-published-input\n"
                )
                self.assertEqual(
                    list(root.glob(f".{target.name}.*.tmp")), []
                )
                self.assertEqual(
                    list(root.glob(f".{target.name}.*.backup")), []
                )

    def test_native_input_foreign_backup_collision_is_never_unlinked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "patron.in"
            target.write_bytes(b"existing-published-input\n")
            foreign = root / ".patron.in.foreign.backup"
            foreign.write_bytes(b"foreign-owner\n")
            identities = (
                SimpleNamespace(hex="owned-temp"),
                SimpleNamespace(hex="foreign"),
            )
            with patch(
                "emuflow.partition_pressure.uuid.uuid4",
                side_effect=identities,
            ), self.assertRaises(FileExistsError):
                _write_patron_native_input(
                    target,
                    self.platform,
                    self.clusters,
                    self.constraints,
                    self.route_constraints,
                    self.model,
                    self.initial,
                    0,
                    6,
                )
            self.assertEqual(
                target.read_bytes(), b"existing-published-input\n"
            )
            self.assertEqual(foreign.read_bytes(), b"foreign-owner\n")
            self.assertFalse((root / ".patron.in.owned-temp.tmp").exists())

    def test_native_input_rollback_failure_retains_recovery_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "patron.in"
            original = b"existing-published-input\n"
            target.write_bytes(original)
            recovery = root / ".patron.in.recovery.backup"
            real_replace = os.replace
            replace_calls = 0
            fsync_calls = 0

            def fail_rollback(source: Path, destination: Path) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    real_replace(source, destination)
                    return
                raise OSError("synthetic rollback replace fault")

            def fail_parent_fsync(_descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("synthetic parent fsync fault")

            identities = (
                SimpleNamespace(hex="owned-temp"),
                SimpleNamespace(hex="recovery"),
            )
            with patch(
                "emuflow.partition_pressure.uuid.uuid4",
                side_effect=identities,
            ), patch(
                "emuflow.partition_pressure.os.fsync",
                side_effect=fail_parent_fsync,
            ), patch(
                "emuflow.partition_pressure.os.replace",
                side_effect=fail_rollback,
            ), self.assertRaises(ValidationError) as caught:
                _write_patron_native_input(
                    target,
                    self.platform,
                    self.clusters,
                    self.constraints,
                    self.route_constraints,
                    self.model,
                    self.initial,
                    0,
                    6,
                )
            self.assertIn(str(recovery), str(caught.exception))
            self.assertEqual(recovery.read_bytes(), original)
            self.assertTrue(target.is_file())
            self.assertNotEqual(target.read_bytes(), original)
            self.assertFalse((root / ".patron.in.owned-temp.tmp").exists())

    def test_compact_pressure_model_rejects_cluster_coverage_corruption(
        self,
    ) -> None:
        variants = []
        missing = copy.deepcopy(self.clusters)
        missing["clusters"][0]["instances"].clear()
        variants.append(("coverage disagrees", missing))
        duplicate = copy.deepcopy(self.clusters)
        duplicate["clusters"][1]["instances"].append(
            duplicate["clusters"][0]["instances"][0]
        )
        variants.append(("duplicated", duplicate))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, self.ir.to_dict())
            for message, clusters in variants:
                with self.subTest(message=message), self.assertRaisesRegex(
                    ValidationError, message
                ):
                    build_partition_pressure_model(
                        load_partition_ir(path),
                        self.platform,
                        clusters,
                        self.constraints,
                        self.timing,
                        self.route_constraints,
                        independent_validation=False,
                    )

    def test_packed_pressure_million_sink_membership_is_fixed_width(
        self,
    ) -> None:
        class LazyClusters(Sequence):
            def __len__(self) -> int:
                return 1_000_001

            def __getitem__(self, index: int) -> dict:
                if index < 0 or index >= len(self):
                    raise IndexError(index)
                return {"cluster": f"c{index:07d}"}

        identities = _PackedStringOrdinalIndex()
        nets = _PackedPressureNetSequence(LazyClusters(), identities)
        nets.append_record(
            "million_fanout",
            "register_output",
            (0,),
            range(1, 1_000_001),
        )
        self.assertEqual(len(nets), 1)
        self.assertEqual(len(nets._sinks), 1_000_000)
        self.assertLess(nets.payload_bytes, 8_100_000)

    def test_packed_pressure_paths_use_fixed_width_relations(self) -> None:
        cluster_identities = _PackedStringOrdinalIndex()
        cluster_identities.append("c000000", 0)
        clusters = [
            {
                "cluster": "c000000",
                "cells": 1,
                "resources": {"lut": 1},
                "fixed_fpga": None,
            }
        ]
        nets = _PackedPressureNetSequence(clusters, cluster_identities)
        nets.append_record("n0", "register_output", (0,), (0,))
        known_nets = _PackedStringOrdinalIndex()
        known_nets.append("n0", 0)
        paths = _PackedPressurePathSequence(known_nets, nets, clusters)
        for index in range(100_000):
            paths.append_record(
                f"p{index:06d}",
                "clk",
                10.0,
                -1.0,
                ("n0",),
                0,
                0,
                True,
            )
        self.assertEqual(len(paths), 100_000)
        self.assertLess(paths.payload_bytes, 12_000_000)
        self.assertEqual(
            paths.native_path(99_999),
            (10.0, -1.0, 0, 0, array("I", [0])),
        )

    def test_packed_conservative_path_discards_partial_endpoint_pair(
        self,
    ) -> None:
        cluster_identities = _PackedStringOrdinalIndex()
        cluster_identities.append("c000000", 0)
        clusters = [{
            "cluster": "c000000",
            "cells": 1,
            "resources": {"lut": 1},
            "fixed_fpga": None,
        }]
        nets = _PackedPressureNetSequence(clusters, cluster_identities)
        nets.append_record("n0", "register_output", (0,), (0,))
        known_nets = _PackedStringOrdinalIndex()
        known_nets.append("n0", 0)
        paths = _PackedPressurePathSequence(known_nets, nets, clusters)
        paths.append_record(
            "top-to-cell",
            "clk",
            10.0,
            -1.0,
            ("n0",),
            None,
            0,
            False,
        )
        self.assertEqual(
            paths.native_path(0),
            (10.0, -1.0, -1, -1, array("I", [0])),
        )
        self.assertEqual(
            paths[0]["transition_model"], "conservative-net-worst-v1"
        )
        self.assertNotIn("start_cluster", paths[0])
        self.assertNotIn("end_cluster", paths[0])

    def test_packed_exact_path_rejects_partial_endpoint_before_mutation(
        self,
    ) -> None:
        cluster_identities = _PackedStringOrdinalIndex()
        cluster_identities.append("c000000", 0)
        clusters = [{
            "cluster": "c000000",
            "cells": 1,
            "resources": {"lut": 1},
            "fixed_fpga": None,
        }]
        nets = _PackedPressureNetSequence(clusters, cluster_identities)
        nets.append_record("n0", "register_output", (0,), (0,))
        known_nets = _PackedStringOrdinalIndex()
        known_nets.append("n0", 0)
        paths = _PackedPressurePathSequence(known_nets, nets, clusters)
        empty_payload_bytes = paths.payload_bytes
        with self.assertRaisesRegex(
            ValidationError,
            "partition pressure exact path endpoints are invalid",
        ):
            paths.append_record(
                "invalid-exact",
                "clk",
                10.0,
                -1.0,
                ("n0",),
                None,
                0,
                True,
            )
        self.assertEqual(len(paths), 0)
        self.assertEqual(paths.payload_bytes, empty_payload_bytes)

    def test_path_objective_uses_maximum_of_per_path_tdm_ratio_sums(self) -> None:
        evaluated = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
        )
        self.assertEqual(
            [path["tdm_ratio_sum"] for path in evaluated["paths"]],
            [2, 2],
        )
        self.assertEqual(evaluated["metrics"]["maximum_path_hops"], 1)
        self.assertEqual(
            evaluated["metrics"]["maximum_path_tdm_ratio_sum"], 2
        )
        _assignment, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            executable=str(patron_refiner()),
            max_moves=0,
        )
        self.assertEqual(trace["final_metrics"]["maximum_path_hops"], 1)
        self.assertEqual(
            trace["final_metrics"]["maximum_path_tdm_ratio_sum"], 2
        )

    def test_model_consumes_indexed_timing_database_without_json_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            tsv_path = root / "paths.tsv"
            database_path = root / "paths.json"
            write_json(ir_path, self.ir.value)
            rows = [STA_PATH_DATABASE_TSV_HEADER]
            for index, path in enumerate(self.timing["paths"]):
                path_id = (
                    f"{path['startpoint']['instance']}/"
                    f"{path['startpoint']['port']}->"
                    f"{path['endpoint']['instance']}/"
                    f"{path['endpoint']['port']}#{index:08d}"
                )
                rows.append(
                    "\t".join(
                        (
                            path_id.encode().hex(),
                            path["clock_domain"].encode().hex(),
                            str(path["clock_period_ns"]),
                            str(path["slack_ns"]),
                            str(path["fixed_delay_ns"]),
                            ",".join(
                                net.encode().hex()
                                for net in path["path_nets"]
                            ),
                        )
                    )
                )
            tsv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            import_sta_path_database_tsv_streaming(
                tsv_path,
                ir_path,
                database_path,
                provider=OPENSTA_PROVIDER,
            )
            manifest = read_json(database_path)
            self.assertNotIn("paths", manifest)
            model = build_partition_pressure_model(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                manifest,
                self.route_constraints,
                independent_validation=True,
                timing_database_path=database_path,
            )
            self.assertEqual(len(model["paths"]), 2)
            self.assertEqual(
                {item["path"] for item in model["paths"]},
                {
                    "u0/O->u1/I#00000000",
                    "u2/O->u3/I#00000001",
                },
            )
            compact_ir_path = root / "ir.emuir.sqlite"
            write_compact_emuir(compact_ir_path, self.ir.to_dict())
            with patch(
                "emuflow.partition_pressure.iter_sta_path_database_paths",
                side_effect=AssertionError(
                    "indexed compact pressure construction decoded paths"
                ),
            ):
                compact_model = build_partition_pressure_model(
                    load_partition_ir(compact_ir_path),
                    self.platform,
                    self.clusters,
                    self.constraints,
                    manifest,
                    self.route_constraints,
                    independent_validation=True,
                    timing_database_path=database_path,
                )
            self.assertIsInstance(
                compact_model["paths"], _PackedPressurePathSequence
            )
            self.assertTrue(compact_model["paths"].indexed_sql_projection)
            compact_logical = dict(compact_model)
            reference_logical = dict(model)
            compact_logical.pop("sources")
            reference_logical.pop("sources")
            for field in ("clusters", "nets", "paths"):
                compact_logical[field] = list(compact_model[field])
                reference_logical[field] = list(model[field])
            self.assertEqual(compact_logical, reference_logical)

    def test_frozen_assignment_rebase_reuses_identical_clusters(self) -> None:
        with patch(
            "emuflow.phase3.build_partition_assignment",
            side_effect=AssertionError(
                "identical frozen clusters rebuilt the full assignment"
            ),
        ):
            rebased = _rebase_patron_initial_assignment(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.initial,
            )
        self.assertEqual(rebased, self.initial)

    def test_v14_selects_changed_candidate_with_better_predictive_timing(self) -> None:
        initial = {
            "provider": "strong-tritonpart",
            "cluster_assignment": {"c0": "a", "c1": "b"},
        }
        candidate = copy.deepcopy(initial)
        candidate["provider"] = "patron-v14"
        candidate["cluster_assignment"] = {"c0": "b", "c1": "a"}
        trace = {
            "initial_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 8,
                "objective_key": [84.0, 189000.0, 5000, 4, 100, 10, 20, 10]
            },
            "final_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 7,
                "objective_key": [83.0, 180000.0, 4900, 4, 100, 10, 20, 10]
            },
        }
        selected, report = _select_patron_timing_assignment(
            initial, candidate, trace
        )
        self.assertIs(selected, candidate)
        self.assertEqual(report["selected"], "candidate")
        self.assertEqual(
            report["provider"], PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER
        )

    def test_v14_selection_ignores_downstream_semantic_diagnostics(self) -> None:
        initial = {
            "provider": "strong-tritonpart",
            "cluster_assignment": {"c0": "a", "c1": "b"},
            "semantic_contract": {
                "metrics": {
                    "logic_segments": 100,
                    "capture_requirements": 80,
                    "transported_cut_nets": 10,
                    "dependency_edges": 5,
                }
            },
        }
        candidate = copy.deepcopy(initial)
        candidate["provider"] = "patron-v14"
        candidate["cluster_assignment"] = {"c0": "b", "c1": "a"}
        candidate["semantic_contract"]["metrics"]["dependency_edges"] = 6
        trace = {
            "initial_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 8,
                "objective_key": [84.0, 189000.0, 5000, 4, 100, 10, 20, 10]
            },
            "final_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 7,
                "objective_key": [83.0, 180000.0, 4900, 4, 100, 10, 20, 10]
            },
        }
        selected, report = _select_patron_timing_assignment(
            initial, candidate, trace
        )
        self.assertIs(selected, candidate)
        self.assertEqual(report["selected"], "candidate")
        self.assertNotIn("semantic_non_regression", report)
        self.assertNotIn("initial_objective", report)
        self.assertNotIn("candidate_objective", report)

    def test_v14_does_not_require_deferred_semantic_contract(self) -> None:
        initial = {
            "provider": "strong-tritonpart",
            "cluster_assignment": {"c0": "a", "c1": "b"},
        }
        candidate = {
            "provider": "patron-v14",
            "cluster_assignment": {"c0": "b", "c1": "a"},
        }
        trace = {
            "initial_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 8,
                "objective_key": [84.0, 189000.0, 5000, 4, 100, 10, 20, 10]
            },
            "final_metrics": {
                "maximum_path_hops": 2,
                "maximum_path_tdm_ratio_sum": 7,
                "objective_key": [83.0, 180000.0, 4900, 4, 100, 10, 20, 10]
            },
        }
        selected, report = _select_patron_timing_assignment(
            initial, candidate, trace
        )
        self.assertIs(selected, candidate)
        self.assertEqual(report["selected"], "candidate")
        self.assertNotIn("semantic_non_regression", report)
        self.assertNotIn("semantic_counts_available", report)

    def test_model_is_source_bound_and_tamper_evident(self) -> None:
        checked = validate_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            self.model,
        )
        self.assertEqual(checked["status"], "pass")

        self.assertEqual(checked["paths"], 2)
        broken = copy.deepcopy(self.model)
        broken["paths"][0]["base_slack_ns"] += 0.25
        with self.assertRaisesRegex(ValidationError, "reconstruction"):
            validate_partition_pressure_model(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.timing,
                self.route_constraints,
                broken,
            )

    def test_physical_hop_guard_configuration_preserves_legacy_contracts(
        self,
    ) -> None:
        legacy = _flow_refinement_configuration(True, 512, version=2)
        ranked = _flow_refinement_configuration(True, 512, version=3)
        guarded = _flow_refinement_configuration(True, 512, version=4)
        self.assertEqual(legacy["maximum_tail_moves"], 16)
        self.assertNotIn("frontier_selection", legacy)
        self.assertEqual(ranked["maximum_tail_moves"], 256)
        self.assertEqual(
            ranked["frontier_selection"],
            "ranked-worst-path-window-v1",
        )
        self.assertNotIn("physical_hop_guard", ranked)
        self.assertEqual(
            guarded["physical_hop_guard"], "scale_ns*route_hops"
        )
        self.assertEqual(guarded["physical_hop_guard_scale_ns"], 5.0)

    def test_physical_hop_guard_is_charged_once_per_route_arc(self) -> None:
        baseline = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
        )
        guarded = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
            physical_hop_guard_scale_ns=5.0,
        )
        baseline_paths = {
            record["path"]: record for record in baseline["paths"]
        }
        for record in guarded["paths"]:
            self.assertAlmostEqual(
                record["transport_delay_ns"]
                - baseline_paths[record["path"]]["transport_delay_ns"],
                5.0,
                places=12,
            )
        with self.assertRaisesRegex(ValidationError, "non-negative"):
            evaluate_partition_pressure(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                self.model,
                self.initial["cluster_assignment"],
                physical_hop_guard_scale_ns=-0.5,
            )

    def test_v7_native_batch_parser_rejects_tamper(self) -> None:
        before = "2 4 1 2 3 4 5 6"
        after = "1 3 1 2 3 4 5 6"
        ranked = "1000000000 3000000000 1 2 3 4 5 6"
        lines = [
            "EMUFLOW_PATRON_OUTPUT_V7",
            "MODE endpoint-exact-critical-flow-v7",
            f"INITIAL {before}",
            f"BATCH 0 1 {before} {after} {ranked}",
            "CHANGE 0 0 0 1",
            f"FINAL {after}",
            "ASSIGN 0 1",
            "ASSIGN 1 0",
            "END",
        ]
        indexes = {"clusters": ["c0", "c1"], "parts": ["a", "b"]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patron.out"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError(
                    "PATRON output must be parsed as a record stream"
                ),
            ):
                assignment, _moves, batches, *_rest = (
                    _parse_patron_native_output(path, indexes)
                )
            self.assertEqual(assignment, {"c0": "b", "c1": "a"})
            self.assertEqual(
                batches[0]["changes"],
                [{"cluster": "c0", "source": "a", "target": "b"}],
            )

            changed = list(lines)
            changed[4] = "CHANGE 0 2 0 1"
            path.write_text("\n".join(changed) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "out of range"):
                _parse_patron_native_output(path, indexes)

            truncated = list(lines)
            truncated[3] = f"BATCH 0 2 {before} {after} {ranked}"
            path.write_text(
                "\n".join(truncated) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "coverage"):
                _parse_patron_native_output(path, indexes)

            attacks = (
                ("assignment-order", lines[:6] + [lines[7], lines[6], "END"], "ASSIGN order"),
                ("assignment-range", lines[:6] + ["ASSIGN 9 1", lines[7], "END"], "out of range"),
                ("assignment-duplicate", lines[:7] + [lines[6], "END"], "duplicate ASSIGN"),
                ("assignment-missing", lines[:7] + ["END"], "coverage"),
                ("missing-end", lines[:-1], "END is missing"),
                ("duplicate-end", lines + ["END"], "after END"),
                ("record-after-end", lines + ["ASSIGN 0 1"], "after END"),
                ("early-end", lines[:6] + ["END", lines[6], lines[7]], "after END"),
            )
            for label, attacked, message in attacks:
                with self.subTest(label=label):
                    path.write_text(
                        "\n".join(attacked) + "\n", encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ValidationError, message):
                        _parse_patron_native_output(path, indexes)

    def test_native_output_packs_lexical_assignment_into_cluster_ordinals(
        self,
    ) -> None:
        lines = [
            "EMUFLOW_PATRON_OUTPUT_V14",
            "MODE endpoint-exact-critical-flow-v14",
            "INITIAL 0 0 0 0 0 0 0 0",
            "FINAL 0 0 0 0 0 0 0 0",
            "SUMMARY 0 0 0 0",
            "ASSIGN 0 0",
            "ASSIGN 1 1",
            "ASSIGN 2 2",
            "END",
        ]
        indexes = {
            "clusters": ["c000002", "c000000", "c000001"],
            "parts": ["F0", "F1", "F2"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patron.out"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            assignment, *_ = _parse_patron_native_output(
                path, indexes, packed_assignment=True
            )
        self.assertNotIsInstance(assignment, dict)
        self.assertEqual(
            dict(assignment.items()),
            {
                "c000000": "F1",
                "c000001": "F2",
                "c000002": "F0",
            },
        )
        self.assertEqual(assignment.packed_payload_bytes, 12)

    def test_shared_bank_pressure_is_reported_but_not_a_partition_gate(self) -> None:
        value = _platform_value(
            "pressure_shared_bank_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=1, latency=0)],
        )
        for fpga in value["fpgas"]:
            fpga["hp_io_bank_count"] = 1
        value["links"][0].update(
            {
                "capacity_sharing": "shared_bidirectional",
                "bank_count": 1,
                "wires_per_bank": 1,
            }
        )
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            self.ir,
            platform,
        )
        clusters = build_clusters(self.ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            self.ir,
            platform,
            clusters,
            constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "b",
                by_instance["u3"]: "a",
            },
            provider="fixture",
            seed=7,
        )
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 1,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            platform,
        )
        model = build_partition_pressure_model(
            self.ir,
            platform,
            clusters,
            constraints,
            self.timing,
            routes,
        )
        initial_evaluation = evaluate_partition_pressure(
            self.ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial["cluster_assignment"],
        )
        self.assertEqual(
            initial_evaluation["metrics"]["transport_capacity_violations"],
            1,
        )
        self.assertEqual(
            initial_evaluation["metrics"]["transport_capacity_excess_bits"],
            1,
        )

        final, trace = run_partition_pressure_native(
            self.ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=0,
        )
        final_evaluation = evaluate_partition_pressure(
            self.ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            final["cluster_assignment"],
        )
        self.assertEqual(
            final_evaluation["metrics"]["transport_capacity_violations"],
            1,
        )
        self.assertEqual(
            final_evaluation["metrics"]["transport_capacity_excess_bits"],
            1,
        )
        self.assertEqual(
            trace["initial_metrics"]["transport_capacity_excess_bits"], 1
        )
        self.assertEqual(
            trace["final_metrics"]["transport_capacity_excess_bits"], 1
        )

    def test_v9_native_input_and_bundle_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native_input = Path(temporary) / "patron-v9.in"
            native_output = Path(temporary) / "patron-v9.out"
            indexes = _write_patron_native_input(
                native_input,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                self.model,
                self.initial,
                0,
                9,
            )
            lines = native_input.read_text(encoding="utf-8").splitlines()
            flow_fields = lines[2].split()
            self.assertEqual(flow_fields[0], "FLOW")
            self.assertEqual(len(flow_fields), 9)
            for index, line in enumerate(lines):
                fields = line.split()
                if fields and fields[0] == "CLUSTER":
                    fields[3] = fields[2]
                    lines[index] = " ".join(fields)
            native_input.write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    str(patron_refiner()),
                    str(native_input),
                    str(native_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                native_output.read_text(encoding="utf-8").splitlines()[0],
                "EMUFLOW_PATRON_OUTPUT_V9",
            )
            (
                cluster_assignment,
                moves,
                batches,
                initial_metrics,
                final_metrics,
                mode,
                output_summary,
            ) = _parse_patron_native_output(native_output, indexes)
        self.assertEqual(mode, "endpoint-exact-critical-flow-v9")
        self.assertTrue(output_summary["detail_retained"])
        self.assertGreater(output_summary["output_bytes"], 0)
        final = build_partition_assignment(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            cluster_assignment,
            provider="patron-endpoint-exact-flow-native-v9",
            seed=self.initial["seed"],
        )
        trace = {
            "schema": "emuflow.partition-pressure-trace/v9",
            "design": self.model["design"],
            "platform": self.model["platform"],
            "provider": "patron-endpoint-exact-flow-native-v9",
            "mode": mode,
            "configuration": {
                "max_moves": 0,
                "max_scalable_sweeps": self.model["configuration"][
                    "max_scalable_sweeps"
                ],
                "scalable_ejection_critical_limit": self.model[
                    "configuration"
                ]["scalable_ejection_critical_limit"],
                "scalable_ejection_donor_limit": self.model["configuration"]
                ["scalable_ejection_donor_limit"],
                "max_scalable_ejections": self.model["configuration"]
                ["max_scalable_ejections"],
                "flow_refinement": _flow_refinement_configuration(
                    True, len(self.model["clusters"]), version=3
                ),
            },
            "model_sha256": _canonical_digest(self.model),
            "initial_assignment_sha256": _canonical_digest(self.initial),
            "initial_metrics": initial_metrics,
            "moves": moves,
            "batches": batches,
            "final_metrics": final_metrics,
            "final_cluster_assignment_sha256": _canonical_digest(
                cluster_assignment
            ),
        }
        checked = validate_partition_pressure_native_bundle(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")

    def test_v10_flow_batch_and_physical_hop_guard_match_small_oracle(
        self,
    ) -> None:
        instance_count = 40
        block_size = 10
        cut_count = 8
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "pressure_flow_oracle",
                    "top": "pressure_flow_oracle",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {
                        "id": f"u{index:03d}",
                        "type": "LUT1",
                        "resources": {"lut": 1},
                    }
                    for index in range(instance_count)
                ],
                "nets": [
                    {
                        "id": f"n{index:03d}",
                        "cut_class": "register_output",
                        "drivers": [
                            _endpoint(
                                f"u{block_size + index:03d}", "O"
                            )
                        ],
                        "sinks": [
                            _endpoint(
                                f"u{2 * block_size + index:03d}", "I"
                            )
                        ],
                    }
                    for index in range(cut_count)
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        value = _platform_value(
            "pressure_flow_oracle_platform",
            ["a", "b", "c", "d"],
            [
                _link("ab", "a", "b", lanes=2, latency=1),
                _link("bc", "b", "c", lanes=2, latency=1),
                _link("cd", "c", "d", lanes=2, latency=1),
            ],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 4,
                "balance_tolerance": 0.5,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        parts = ["a", "b", "c", "d"]
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            {
                by_instance[f"u{index:03d}"]: parts[index // block_size]
                for index in range(instance_count)
            },
            provider="fixture",
            seed=1,
        )
        route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 3,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_flow_oracle",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": f"p{index:03d}",
                    "startpoint": _endpoint(
                        f"u{block_size + index:03d}", "O"
                    ),
                    "endpoint": _endpoint(
                        f"u{2 * block_size + index:03d}", "I"
                    ),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": [f"n{index:03d}"],
                    "normalized_slack": 1.0,
                }
                for index in range(cut_count)
            ],
        }
        model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
        )
        movable = [
            by_instance[f"u{block_size + index:03d}"]
            for index in range(cut_count)
        ]

        flow_only_best_rank = None
        for targets in itertools.product(("c", "d"), repeat=cut_count):
            trial = dict(initial["cluster_assignment"])
            trial.update(dict(zip(movable, targets)))
            try:
                evaluation = evaluate_partition_pressure(
                    ir,
                    platform,
                    clusters,
                    constraints,
                    route_constraints,
                    model,
                    trial,
                    include_tdm_wait=True,
                )
            except ValidationError:
                continue
            objective = evaluation["metrics"]["objective_key"]
            rank = (0, 0) + tuple(
                round(float(value) / 1.0e-9)
                if index < 2
                else round(float(value))
                for index, value in enumerate(objective)
            )
            flow_only_best_rank = (
                rank
                if flow_only_best_rank is None
                else min(flow_only_best_rank, rank)
            )
        self.assertIsNotNone(flow_only_best_rank)

        # Every timing path can be made local in this compact fixture.  Since
        # transport delay is non-negative, the following rank is also a
        # constructive lower bound on all eight objective components.
        global_best_rank = (0, 0, -1000000000, 0, 0, 1, 0, 0, 0, 0)

        results = []
        for _repeat in range(2):
            final, trace = run_partition_pressure_native(
                ir,
                platform,
                clusters,
                constraints,
                route_constraints,
                model,
                initial,
                executable=str(patron_refiner()),
                max_moves=0,
                flow_refinement=True,
            )
            self.assertEqual(len(trace["batches"]), 1)
            self.assertEqual(
                tuple(trace["batches"][0]["ranked_objective_key"]),
                global_best_rank,
            )
            self.assertLess(
                global_best_rank,
                flow_only_best_rank,
            )
            for index in range(cut_count):
                launch = by_instance[
                    f"u{block_size + index:03d}"
                ]
                capture = by_instance[
                    f"u{2 * block_size + index:03d}"
                ]
                self.assertEqual(
                    final["cluster_assignment"][launch],
                    final["cluster_assignment"][capture],
                )
            validation = validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                final,
                trace,
            )
            self.assertEqual(validation["status"], "pass")
            results.append((final, trace))
        self.assertEqual(results[0], results[1])

    def test_endpoint_exact_fanout_does_not_charge_an_unrelated_sink(self) -> None:
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "fanout_pressure",
                    "top": "fanout_pressure",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {"id": item, "type": "LUT1", "resources": {"lut": 1}}
                    for item in ("driver", "local", "remote", "remote_peer")
                ],
                "nets": [
                    {
                        "id": "fanout",
                        "cut_class": "register_output",
                        "drivers": [_endpoint("driver", "O")],
                        "sinks": [
                            _endpoint("local", "I"),
                            _endpoint("remote", "I"),
                            _endpoint("remote_peer", "I"),
                        ],
                    }
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        platform = Platform.from_dict(
            _platform_value(
                "fanout_platform",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=1, latency=1),
                    _link("bc", "b", "c", lanes=1, latency=1),
                ],
            )
        )
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 2.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        cluster_by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        assignment = {
            cluster_by_instance["driver"]: "a",
            cluster_by_instance["local"]: "a",
            cluster_by_instance["remote"]: "c",
            cluster_by_instance["remote_peer"]: "c",
        }
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 2,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "fanout_pressure",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 10.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "to-local",
                    "startpoint": _endpoint("driver", "O"),
                    "endpoint": _endpoint("local", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 5.0,
                    "fixed_delay_ns": 5.0,
                    "path_nets": ["fanout"],
                    "normalized_slack": 0.5,
                },
                {
                    "id": "to-remote",
                    "startpoint": _endpoint("driver", "O"),
                    "endpoint": _endpoint("remote", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 5.0,
                    "fixed_delay_ns": 5.0,
                    "path_nets": ["fanout"],
                    "normalized_slack": 0.5,
                },
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        evaluated = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            assignment,
        )
        paths = {record["path"]: record for record in evaluated["paths"]}
        self.assertEqual(paths["to-local"]["transport_delay_ns"], 0.0)
        self.assertGreater(paths["to-remote"]["transport_delay_ns"], 0.0)
        reduced_fanout_assignment = dict(assignment)
        reduced_fanout_assignment[cluster_by_instance["remote_peer"]] = "a"
        reduced_fanout = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            reduced_fanout_assignment,
        )
        reduced_paths = {
            record["path"]: record for record in reduced_fanout["paths"]
        }
        self.assertAlmostEqual(
            paths["to-remote"]["transport_delay_ns"]
            - reduced_paths["to-remote"]["transport_delay_ns"],
            model["configuration"]["boundary_fanout_penalty_scale_ns"]
            * (math.log2(3.0) - math.log2(2.0)),
        )
        self.assertEqual(
            paths["to-local"]["transition_model"],
            "endpoint-exact-reverse-chain-v1",
        )

        conservative_timing = copy.deepcopy(timing)
        for record in conservative_timing["paths"]:
            record.pop("startpoint")
            record.pop("endpoint")
        conservative_model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            conservative_timing,
            routes,
        )
        conservative = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            conservative_model,
            assignment,
        )
        conservative_paths = {
            record["path"]: record for record in conservative["paths"]
        }
        self.assertEqual(
            conservative_paths["to-local"]["transport_delay_ns"],
            conservative_paths["to-remote"]["transport_delay_ns"],
        )
        self.assertGreater(
            conservative_paths["to-local"]["transport_delay_ns"], 0.0
        )

    def test_global_best_move_improves_critical_path_and_replays(self) -> None:
        before = evaluate_partition_pressure(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial["cluster_assignment"],
        )
        final, trace = refine_partition_pressure_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
        )
        self.assertGreaterEqual(len(trace["moves"]), 1)
        self.assertGreater(
            trace["final_metrics"]["worst_normalized_slack"],
            before["metrics"]["worst_normalized_slack"],
        )
        checked = validate_partition_pressure_trace(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")
        repeated, repeated_trace = refine_partition_pressure_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
        )
        self.assertEqual(repeated, final)
        self.assertEqual(repeated_trace, trace)

    def test_shared_capacity_direction_changes_pressure(self) -> None:
        shared_platform_value = _platform_value(
            "shared_pressure",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=1, latency=1)],
        )
        shared_platform_value["links"][0]["capacity_sharing"] = (
            "shared_bidirectional"
        )
        shared = Platform.from_dict(shared_platform_value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            self.ir,
            shared,
        )
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
            },
            shared,
        )
        model = build_partition_pressure_model(
            self.ir,
            shared,
            self.clusters,
            constraints,
            self.timing,
            routes,
        )
        evaluation = evaluate_partition_pressure(
            self.ir,
            shared,
            self.clusters,
            constraints,
            routes,
            model,
            self.initial["cluster_assignment"],
        )
        self.assertEqual(len(evaluation["capacity_domains"]), 1)
        self.assertEqual(
            evaluation["metrics"]["maximum_capacity_domain_load"], 2
        )
        self.assertEqual(
            evaluation["metrics"]["maximum_predicted_tdm_ratio"], 2
        )

    def test_reference_runner_writes_a_replayable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = {
                "ir.json": self.ir.value,
                "platform.json": self.platform.to_dict(),
                "clusters.json": self.clusters,
                "constraints.json": self.constraints,
                "timing.json": self.timing,
                "routes.json": self.route_constraints,
                "assignment.json": self.initial,
            }
            for name, value in inputs.items():
                write_json(root / name, value)
            report = run_partition_pressure_reference(
                root / "ir.json",
                root / "platform.json",
                root / "clusters.json",
                root / "constraints.json",
                root / "timing.json",
                root / "routes.json",
                root / "assignment.json",
                root / "out",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                sorted(path.name for path in (root / "out").iterdir()),
                [
                    "assignment.json",
                    "partition_pressure_model.json",
                    "partition_pressure_report.json",
                    "partition_pressure_trace.json",
                ],
            )

    def test_native_matches_exhaustive_move_for_move(self) -> None:
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            executable=str(patron_refiner()),
        )
        checked = validate_partition_pressure_native_against_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")
        self.assertGreaterEqual(checked["moves"], 1)
        bundle = validate_partition_pressure_native_bundle(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.timing,
            self.route_constraints,
            self.model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(
            bundle["qualification"], "move-for-move-exhaustive"
        )

    def test_native_patron_timeout_is_enforced(self) -> None:
        class TimeoutProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.killed = False
                self.wait_timeouts = []

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                if not self.killed:
                    raise subprocess.TimeoutExpired(
                        cmd=["emuflow_patron_refiner"], timeout=timeout
                    )
                return -9

            def kill(self):
                self.killed = True

        process = TimeoutProcess()
        with patch(
            "emuflow.partition_pressure.subprocess.Popen",
            return_value=process,
        ) as popen:
            with self.assertRaisesRegex(
                ValidationError,
                "native PATRON exceeded timeout of 7 seconds",
            ):
                run_partition_pressure_native(
                    self.ir,
                    self.platform,
                    self.clusters,
                    self.constraints,
                    self.route_constraints,
                    self.model,
                    self.initial,
                    executable="/bin/true",
                    timeout_seconds=7,
                )
        self.assertEqual(process.wait_timeouts, [7, None])
        self.assertTrue(process.killed)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)

    def test_native_patron_timeout_must_be_positive(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "native PATRON timeout is invalid"
        ):
            run_partition_pressure_native(
                self.ir,
                self.platform,
                self.clusters,
                self.constraints,
                self.route_constraints,
                self.model,
                self.initial,
                executable="/bin/true",
                timeout_seconds=0,
            )

    def test_native_legacy_fallback_matches_exhaustive(self) -> None:
        timing = copy.deepcopy(self.timing)
        for path in timing["paths"]:
            path.pop("startpoint")
            path.pop("endpoint")
        model = build_partition_pressure_model(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            timing,
            self.route_constraints,
        )
        self.assertEqual(
            {path["transition_model"] for path in model["paths"]},
            {"conservative-net-worst-v1"},
        )
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            model,
            self.initial,
            executable=str(patron_refiner()),
        )
        checked = validate_partition_pressure_native_against_exhaustive(
            self.ir,
            self.platform,
            self.clusters,
            self.constraints,
            self.route_constraints,
            model,
            self.initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")

    def test_exact_mode_uses_an_atomic_ejection_when_balance_blocks_moves(
        self,
    ) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 0.0,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "a",
                by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=7,
        )
        model = build_partition_pressure_model(
            self.ir,
            self.platform,
            clusters,
            constraints,
            self.timing,
            self.route_constraints,
        )
        final, trace = run_partition_pressure_native(
            self.ir,
            self.platform,
            clusters,
            constraints,
            self.route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
        )
        self.assertEqual(trace["mode"], "endpoint-exact-global-best-v6")
        self.assertGreaterEqual(len(trace["moves"]), 1)
        self.assertEqual(trace["moves"][0]["kind"], "ejection")
        self.assertIsNotNone(trace["moves"][0]["partner"])
        checked = validate_partition_pressure_native_against_exhaustive(
            self.ir,
            self.platform,
            clusters,
            constraints,
            self.route_constraints,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")

    def test_phase3_patron_provider_refines_imported_tritonpart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            solution_path = root / "initial.part.2"
            write_json(ir_path, self.ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(timing_path, self.timing)
            raw_routes = {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 8,
                "tdm_ratio_quantum": 1,
            }
            write_json(route_path, raw_routes)
            ordered = sorted(
                self.initial["cluster_assignment"]
            )
            part = {"a": 0, "b": 1}
            solution_path.write_text(
                "\n".join(
                    str(part[self.initial["cluster_assignment"][cluster]])
                    for cluster in ordered
                )
                + "\n",
                encoding="utf-8",
            )
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                constraints_path=None,
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                cut_mode="sequential-only",
                patron_algorithm_version=6,
                tritonpart_solution=solution_path,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                retain_diagnostics=True,
            )
            trace = (root / "phase3/patron/refinement_trace.json")
            self.assertEqual(report["status"], "pass")
            self.assertTrue(trace.is_file())
            self.assertTrue(
                (root / "phase3/patron/candidate_assignment.json").is_file()
            )
            self.assertEqual(
                report["provider"], "patron-endpoint-exact-native-v6"
            )
            compact_ir_path = root / "ir.emuir.sqlite"
            write_compact_emuir(compact_ir_path, self.ir.to_dict())
            compact_report = run_phase3(
                compact_ir_path,
                platform_path,
                root / "phase3-compact-metric",
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                cut_mode="sequential-only",
                patron_algorithm_version=6,
                tritonpart_solution=solution_path,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
            )
            self.assertEqual(compact_report["status"], "pass")
            compact_runtime = read_json(
                root / "phase3-compact-metric/phase3-runtime.json"
            )
            observations = compact_runtime["observations"]
            self.assertGreater(
                observations["pressure_compact_net_payload_bytes"], 0
            )
            self.assertGreater(
                observations["pressure_compact_path_payload_bytes"], 0
            )
            self.assertEqual(
                observations["pressure_indexed_sql_projection"], 0
            )
            self.assertNotIn("pressure_model_payload_bytes", observations)
            baseline = promote_patron_baseline(
                ir_path, platform_path, root / "phase3"
            )
            restored = read_json(root / "phase3/assignment.json")
            self.assertEqual(
                baseline["provider"], "tritonpart-openroad-hypergraph-v1"
            )
            self.assertEqual(
                restored["cluster_assignment"],
                self.initial["cluster_assignment"],
            )
            frozen_initial_path = root / "frozen-initial.json"
            write_json(frozen_initial_path, self.initial)
            reused = run_phase3(
                ir_path,
                platform_path,
                root / "phase3-reused",
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                cut_mode="sequential-only",
                patron_algorithm_version=6,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                patron_initial_assignment_path=frozen_initial_path,
                retain_diagnostics=True,
            )
            self.assertEqual(reused["status"], "pass")
            self.assertNotIn("tritonpart", reused["artifacts"])
            self.assertEqual(
                reused["algorithm_validation"]["status"], "pass"
            )
            self.assertFalse(reused["hop_refinement"]["enabled"])
            self.assertEqual(
                reused["hop_refinement"]["reason"],
                "patron-native-hop-contract-audited",
            )
            self.assertFalse((root / "phase3-reused/hop-refinement").exists())
            self.assertFalse(
                (
                    root
                    / "phase3-reused/patron/initial-hop-refinement"
                ).exists()
            )
            compact = run_phase3(
                ir_path,
                platform_path,
                root / "phase3-compact",
                constraints_path=None,
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                cut_mode="sequential-only",
                patron_algorithm_version=6,
                tritonpart_solution=solution_path,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                retain_diagnostics=False,
                retain_patron_baseline=True,
            )
            self.assertEqual(compact["status"], "pass")
            self.assertFalse((root / "phase3-compact/patron").exists())
            self.assertTrue(
                (root / "phase3-compact/patron-initial-assignment.json").is_file()
            )
            self.assertTrue(
                (
                    root
                    / "phase3-compact/patron-initial-hop-refinement.json"
                ).is_file()
            )
            compact_baseline = promote_patron_baseline(
                ir_path, platform_path, root / "phase3-compact"
            )
            self.assertEqual(
                compact_baseline["provider"],
                "tritonpart-openroad-hypergraph-v1",
            )
            legacy = copy.deepcopy(self.initial)
            legacy["cluster_assignment"] = {
                f"legacy-{index}": fpga
                for index, fpga in enumerate(
                    self.initial["cluster_assignment"].values()
                )
            }
            legacy_path = root / "legacy-cluster-ids.json"
            write_json(legacy_path, legacy)
            rebased = run_phase3(
                ir_path,
                platform_path,
                root / "phase3-rebased",
                min_used_fpgas=2,
                balance_tolerance=1.0,
                provider="patron",
                cut_mode="sequential-only",
                patron_algorithm_version=6,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                patron_initial_assignment_path=legacy_path,
                retain_diagnostics=True,
            )
            self.assertEqual(rebased["status"], "pass")
            rebased_initial = read_json(
                root / "phase3-rebased/patron/initial_assignment.json"
            )
            self.assertEqual(
                rebased_initial["instance_assignment"],
                self.initial["instance_assignment"],
            )
            self.assertEqual(
                set(rebased_initial["cluster_assignment"]),
                set(self.initial["cluster_assignment"]),
            )

            with (
                patch(
                    "emuflow.partition_pressure._canonical_digest",
                    side_effect=AssertionError(
                        "production Phase 3 entered canonical JSON hashing"
                    ),
                ),
                patch(
                    "emuflow.partition_pressure.validate_partition_artifacts",
                    side_effect=AssertionError(
                        "managed producer entered the full assignment validator"
                    ),
                ),
                patch(
                    "emuflow.partition_pressure.validate_partition_pressure_model",
                    side_effect=AssertionError(
                        "managed producer rebuilt the pressure model"
                    ),
                ),
                patch(
                    "emuflow.phase3.validate_partition_pressure_native_bundle",
                    side_effect=AssertionError(
                        "managed producer entered offline deep replay"
                    ),
                ),
            ):
                production = run_phase3(
                    ir_path,
                    platform_path,
                    root / "phase3-managed",
                    min_used_fpgas=2,
                    balance_tolerance=1.0,
                    provider="patron",
                    cut_mode="sequential-only",
                    patron_algorithm_version=6,
                    tritonpart_solution=solution_path,
                    route_constraints_path=route_path,
                    timing_database_path=timing_path,
                    patron_refiner=str(patron_refiner()),
                )
            self.assertEqual(production["status"], "pass")
            self.assertEqual(
                production["algorithm_validation"]["qualification"],
                "online-native-output-contract",
            )
            self.assertEqual(
                production["algorithm_validation"]["batch_changes"], 0
            )
            self.assertGreater(
                production["algorithm_validation"]["native_output_bytes"], 0
            )
            self.assertEqual(
                production["patron_diagnostics"]["storage"], "not-persisted"
            )
            for relative in (
                "pressure_model.json",
                "refinement_trace.json",
                "initial_assignment.json",
                "candidate_assignment.json",
            ):
                self.assertFalse(
                    (root / "phase3-managed/patron" / relative).exists()
                )
            self.assertFalse((root / "phase3-managed/patron").exists())

    def test_phase3_patron_consumes_generalized_static_exact_contract(
        self,
    ) -> None:
        ir = _ir()
        ir.value["nets"][0]["cut_class"] = "combinational"
        ir.value["clocks"] = [
            {
                "id": "clk",
                "name": "clk",
                "source_port": "clk",
                "period_ns": None,
            }
        ]
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
                "fixed": [
                    {"instance": "u0", "fpga": "a"},
                    {"instance": "u1", "fpga": "b"},
                ],
            },
            ir,
            self.platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=8,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        initial = build_partition_assignment(
            ir,
            self.platform,
            clusters,
            constraints,
            {
                cluster_for["u0"]: "a",
                cluster_for["u1"]: "b",
                cluster_for["u2"]: "a",
                cluster_for["u3"]: "b",
            },
            provider="fixture-static-exact-v2",
            seed=7,
        )
        # Managed production inputs defer this large downstream payload until
        # PATRON has selected the final assignment.
        initial.pop("semantic_contract")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            constraints_path = root / "constraints.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            initial_path = root / "initial.json"
            initial_clusters_path = root / "initial-clusters.json"
            write_json(ir_path, ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(constraints_path, constraints)
            write_json(timing_path, self.timing)
            write_json(
                route_path,
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                    "tdm_ratio_quantum": 1,
                },
            )
            write_json(initial_path, initial)
            write_json(initial_clusters_path, clusters)
            with patch(
                "emuflow.phase3.build_clusters",
                side_effect=AssertionError(
                    "managed PATRON rebuilt validated initial clusters"
                ),
            ):
                report = run_phase3(
                    ir_path,
                    platform_path,
                    root / "phase3",
                    constraints_path=constraints_path,
                    provider="patron",
                    route_constraints_path=route_path,
                    timing_database_path=timing_path,
                    patron_refiner=str(patron_refiner()),
                    patron_algorithm_version=14,
                    patron_initial_assignment_path=initial_path,
                    patron_initial_clusters_path=initial_clusters_path,
                    cut_mode=CUT_MODE_STATIC_EXACT,
                    max_cross_fpga_dependency_depth=8,
                    static_exact_candidate_policy=(
                        STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                    ),
                    retain_diagnostics=False,
                )
            assignment = read_json(root / "phase3/assignment.json")
            runtime = read_json(root / "phase3/phase3-runtime.json")
            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["patron_initial_clusters_reused"])
            self.assertEqual(
                report["cut_mode"], "static-exact-combinational"
            )
            self.assertEqual(
                assignment["metrics"]["combinational_cut_nets"], 1
            )
            self.assertIn("semantic_contract", assignment)
            self.assertEqual(
                assignment["metrics"][
                    "maximum_combinational_dependency_depth"
                ],
                1,
            )
            self.assertEqual(
                report["algorithm_validation"]["status"], "pass"
            )
            self.assertEqual(report["patron_algorithm_version"], 14)
            self.assertEqual(
                report["algorithm_validation"][
                    "candidate_selection"
                ]["provider"],
                PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
            )
            step_names = [step["name"] for step in runtime["steps"]]
            self.assertLess(
                step_names.index("patron.release_pressure_model"),
                step_names.index("patron.build_candidate_assignment"),
            )
            self.assertLess(
                step_names.index("patron.release_post_selection_state"),
                step_names.index("publish.write_assignment_index"),
            )
            self.assertEqual(
                runtime["observations"][
                    "pressure_model_released_before_candidate_assignment"
                ],
                1,
            )

    def test_phase3_patron_v14_initializes_on_selected_generalized_graph(
        self,
    ) -> None:
        ir = _ir()
        ir.value["nets"][0]["cut_class"] = "combinational"
        ir.value["clocks"] = [
            {
                "id": "clk",
                "name": "clk",
                "source_port": "clk",
                "period_ns": None,
            }
        ]
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            ir,
            self.platform,
        )
        generalized_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        generalized_map = {
            cluster["id"]: (
                "a" if "u0" in cluster["instances"]
                else "b"
            )
            for cluster in generalized_clusters["clusters"]
        }
        generalized = build_partition_assignment(
            ir,
            self.platform,
            generalized_clusters,
            constraints,
            generalized_map,
            provider="fixture-generalized-tritonpart",
            seed=7,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            constraints_path = root / "constraints.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            write_json(ir_path, ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(constraints_path, constraints)
            write_json(timing_path, self.timing)
            write_json(
                route_path,
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                    "tdm_ratio_quantum": 1,
                },
            )
            with patch(
                "emuflow.phase3.run_tritonpart", return_value=generalized
            ) as tritonpart_run:
                report = run_phase3(
                    ir_path,
                    platform_path,
                    root / "phase3",
                    constraints_path=constraints_path,
                    provider="patron",
                    route_constraints_path=route_path,
                    timing_database_path=timing_path,
                    patron_refiner=str(patron_refiner()),
                    patron_algorithm_version=14,
                    cut_mode=CUT_MODE_STATIC_EXACT,
                    retain_diagnostics=False,
                    patron_max_moves=0,
                )
            assignment = read_json(root / "phase3/assignment.json")
            self.assertEqual(
                report["patron_initialization"],
                "native-generalized-tritonpart-net-cut-v5",
            )
            self.assertNotIn(
                "objective_encoding", tritonpart_run.call_args.kwargs
            )
            self.assertEqual(
                tritonpart_run.call_args.args[2]["policy"].get(
                    "cut_mode", CUT_MODE_SEQUENTIAL_ONLY
                ),
                CUT_MODE_STATIC_EXACT,
            )
            self.assertEqual(
                assignment["instance_assignment"],
                generalized["instance_assignment"],
            )
            self.assertGreater(
                assignment["metrics"]["combinational_cut_nets"], 0
            )
            self.assertEqual(
                report["algorithm_validation"]["status"], "pass"
            )
            self.assertEqual(report["patron_algorithm_version"], 14)
            self.assertEqual(
                report["algorithm_validation"][
                    "candidate_selection"
                ]["provider"],
                PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
            )

    def test_phase3_v14_explicit_solution_uses_static_exact_cold_start(self) -> None:
        ir = _ir()
        ir.value["nets"][0]["cut_class"] = "combinational"
        ir.value["clocks"] = [
            {
                "id": "clk",
                "name": "clk",
                "source_port": "clk",
                "period_ns": None,
            }
        ]
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 1.0,
            },
            ir,
            self.platform,
        )
        static_exact_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        initial_map = {
            cluster["id"]: ("a" if index % 2 == 0 else "b")
            for index, cluster in enumerate(
                static_exact_clusters["clusters"]
            )
        }
        initial = build_partition_assignment(
            ir,
            self.platform,
            static_exact_clusters,
            constraints,
            initial_map,
            provider="fixture-static-exact",
            seed=7,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "ir.json"
            platform_path = root / "platform.json"
            constraints_path = root / "constraints.json"
            timing_path = root / "timing.json"
            route_path = root / "routes.json"
            solution_path = root / "static-exact.part.2"
            write_json(ir_path, ir.value)
            write_json(platform_path, self.platform.to_dict())
            write_json(constraints_path, constraints)
            write_json(timing_path, self.timing)
            write_json(
                route_path,
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 8,
                    "tdm_ratio_quantum": 1,
                },
            )
            part = {"a": 0, "b": 1}
            solution_path.write_text(
                "\n".join(
                    str(part[initial_map[cluster_id]])
                    for cluster_id in sorted(initial_map)
                )
                + "\n",
                encoding="utf-8",
            )
            report = run_phase3(
                ir_path,
                platform_path,
                root / "phase3",
                constraints_path=constraints_path,
                provider="patron",
                tritonpart_solution=solution_path,
                route_constraints_path=route_path,
                timing_database_path=timing_path,
                patron_refiner=str(patron_refiner()),
                patron_algorithm_version=14,
                cut_mode=CUT_MODE_STATIC_EXACT,
                retain_diagnostics=False,
            )
            assignment = read_json(root / "phase3/assignment.json")
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["patron_initialization"],
                "caller-supplied-generalized-tritonpart-solution-v1",
            )
            self.assertEqual(
                report["algorithm_validation"]["initial_assignment"][
                    "cut_nets"
                ],
                initial["metrics"]["cut_nets"],
            )
            self.assertIn("semantic_contract", assignment)

    def test_scalable_native_sweep_improves_a_sparse_chain(self) -> None:
        ir = _chain_ir(300)
        value = _platform_value(
            "pressure_chain_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=8, latency=1)],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 0.25,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            {
                cluster["id"]: ("a" if index % 2 == 0 else "b")
                for index, cluster in enumerate(clusters["clusters"])
            },
            provider="fixture",
            seed=1,
        )
        route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_chain",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": f"p{index:05d}",
                    "startpoint": _endpoint(f"u{index:05d}", "O"),
                    "endpoint": _endpoint(f"u{index + 1:05d}", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": [f"n{index:05d}"],
                    "normalized_slack": 1.0,
                }
                for index in range(299)
            ],
        }
        model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
        )
        before = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial["cluster_assignment"],
        )
        with patch.dict(
            "os.environ",
            {
                "EMUFLOW_PATRON_FLOW_APPLY": "1",
                "EMUFLOW_PATRON_FLOW_DISTANCE": "999999",
            },
        ):
            final, trace = run_partition_pressure_native(
                ir,
                platform,
                clusters,
                constraints,
                route_constraints,
                model,
                initial,
                executable=str(patron_refiner()),
                max_moves=64,
            )
        after = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            final["cluster_assignment"],
        )
        self.assertEqual(
            trace["mode"], "endpoint-exact-critical-ejection-v6"
        )
        self.assertGreater(len(trace["moves"]), 0)
        self.assertEqual(trace["configuration"]["max_scalable_sweeps"], 4)
        scalable_checked = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(scalable_checked["status"], "pass")
        bundle = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(
            bundle["qualification"],
            "linear-transition-and-endpoint-reconstruction",
        )
        self.assertLessEqual(
            bundle["maximum_endpoint_relative_objective_error"],
            1.0e-12,
        )
        tampered = copy.deepcopy(trace)
        tampered["moves"][0]["target"] = tampered["moves"][0]["source"]
        with self.assertRaisesRegex(ValidationError, "transition"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                final,
                tampered,
            )
        endpoint_tampered = copy.deepcopy(trace)
        endpoint_tampered["final_metrics"]["objective_key"][1] += 1.0e-4
        with self.assertRaisesRegex(ValidationError, "endpoint objective"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                final,
                endpoint_tampered,
            )
        self.assertLess(
            after["metrics"]["objective_key"],
            before["metrics"]["objective_key"],
        )

        flow_final, flow_trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=0,
            flow_refinement=True,
        )
        self.assertEqual(
            flow_trace["mode"], "endpoint-exact-critical-flow-v10"
        )
        self.assertEqual(
            flow_trace["configuration"]["flow_refinement"][
                "frontier_selection"
            ],
            "ranked-worst-path-window-v1",
        )
        self.assertTrue(
            flow_trace["configuration"]["flow_refinement"]["enabled"]
        )
        self.assertEqual(
            flow_trace["configuration"]["flow_refinement"][
                "physical_hop_guard_scale_ns"
            ],
            5.0,
        )
        flow_bundle = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
            model,
            initial,
            flow_final,
            flow_trace,
        )
        self.assertEqual(flow_bundle["status"], "pass")
        v9_final, v9_trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=0,
            algorithm_version=9,
        )
        self.assertEqual(v9_trace["mode"], "endpoint-exact-critical-flow-v9")
        self.assertEqual(v9_trace["configuration"]["algorithm_version"], 9)
        self.assertNotIn(
            "physical_hop_guard",
            v9_trace["configuration"]["flow_refinement"],
        )
        self.assertEqual(
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                v9_final,
                v9_trace,
            )["status"],
            "pass",
        )
        flow_config_tampered = copy.deepcopy(flow_trace)
        flow_config_tampered["configuration"]["flow_refinement"][
            "corridor_distance"
        ] += 1
        with self.assertRaisesRegex(ValidationError, "trace bounds"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                flow_final,
                flow_config_tampered,
            )
        guard_tampered = copy.deepcopy(flow_trace)
        guard_tampered["configuration"]["flow_refinement"][
            "physical_hop_guard_scale_ns"
        ] = 0.0
        with self.assertRaisesRegex(ValidationError, "trace bounds"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                flow_final,
                guard_tampered,
            )

    def test_scalable_multipass_revisits_capacity_blocked_cluster(self) -> None:
        ir = _capacity_release_ir()
        value = _platform_value(
            "pressure_capacity_release_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=8, latency=1)],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fixed": [
                    {"instance": "u00000", "fpga": "a"},
                    {"instance": "u00002", "fpga": "b"},
                ],
                "min_used_fpgas": 2,
                "balance_tolerance": 0.01,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        self.assertEqual(len(clusters["clusters"]), 300)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        mapping = {
            cluster["id"]: (
                "a" if int(cluster["instances"][0][1:]) % 2 == 0 else "b"
            )
            for cluster in clusters["clusters"]
        }
        mapping[by_instance["u00002"]] = "b"
        mapping[by_instance["u00003"]] = "a"
        mapping[by_instance["u00005"]] = "a"
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            mapping,
            provider="fixture",
            seed=1,
        )
        route_constraints = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_capacity_release",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": f"p{index:05d}",
                    "startpoint": _endpoint(f"u{index:05d}", "O"),
                    "endpoint": _endpoint(f"u{index + 1:05d}", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": [f"n{index:05d}"],
                    "normalized_slack": 1.0,
                }
                for index in (0, 2)
            ],
        }
        model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            route_constraints,
        )
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=8,
        )
        self.assertEqual(
            [
                (move["sweep"], move["cluster"], move["source"], move["target"])
                for move in trace["moves"]
            ],
            [
                (0, by_instance["u00003"], "a", "b"),
                (1, by_instance["u00001"], "b", "a"),
            ],
        )
        checked = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(checked["status"], "pass")
        skipped_sweep = copy.deepcopy(trace)
        skipped_sweep["moves"][1]["sweep"] = 2
        with self.assertRaisesRegex(ValidationError, "sweep"):
            validate_partition_pressure_scalable_trace(
                ir,
                platform,
                clusters,
                constraints,
                route_constraints,
                model,
                initial,
                final,
                skipped_sweep,
            )
        with self.assertRaisesRegex(ValidationError, "transition"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                route_constraints,
                model,
                initial,
                final,
                skipped_sweep,
            )

    def test_scalable_ejection_escapes_an_exact_balance_cork(self) -> None:
        ir = _capacity_release_ir()
        value = _platform_value(
            "pressure_ejection_platform",
            ["a", "b"],
            [_link("ab", "a", "b", lanes=8, latency=1)],
        )
        for fpga in value["fpgas"]:
            fpga["capacity"] = {"lut": 1000, "ff": 1000}
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 2,
                "balance_tolerance": 0.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        mapping = {
            cluster["id"]: (
                "a" if int(cluster["instances"][0][1:]) % 2 == 0 else "b"
            )
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            mapping,
            provider="fixture",
            seed=1,
        )
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_capacity_release",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u00000", "O"),
                    "endpoint": _endpoint("u00001", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["n00000"],
                    "normalized_slack": 1.0,
                }
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        before = evaluate_partition_pressure(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial["cluster_assignment"],
        )
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
        )
        self.assertEqual(
            trace["mode"], "endpoint-exact-critical-ejection-v6"
        )
        self.assertGreaterEqual(len(trace["moves"]), 1)
        self.assertEqual(trace["moves"][0]["kind"], "ejection")
        self.assertLess(
            trace["final_metrics"]["objective_key"],
            before["metrics"]["objective_key"],
        )
        replay = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(replay["status"], "pass")
        bundle = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            routes,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(bundle["status"], "pass")
        tampered = copy.deepcopy(trace)
        tampered["moves"][0]["partner_target"] = (
            tampered["moves"][0]["partner_source"]
        )
        with self.assertRaisesRegex(ValidationError, "transition"):
            validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing,
                routes,
                model,
                initial,
                final,
                tampered,
            )

    def test_scalable_ejection_can_release_into_a_third_block(self) -> None:
        raw_ir = copy.deepcopy(_capacity_release_ir().value)
        raw_ir["design"]["name"] = "pressure_third_block_ejection"
        raw_ir["design"]["top"] = "pressure_third_block_ejection"
        raw_ir["nets"] = raw_ir["nets"][:1]
        resources = {
            "u00000": {"lut": 10},
            "u00001": {"lut": 0},
            "u00003": {"lut": 1, "ff": 10},
            "u00004": {"lut": 10, "ff": 10},
        }
        for instance in raw_ir["instances"]:
            instance["resources"] = resources.get(
                instance["id"], {"lut": 1}
            )
        ir = EmuIR(raw_ir)
        value = _platform_value(
            "pressure_third_block_ejection_platform",
            ["a", "b", "c"],
            [
                _link("ab", "a", "b", lanes=8, latency=1),
                _link("ac", "a", "c", lanes=8, latency=50),
                _link("bc", "b", "c", lanes=8, latency=50),
            ],
        )
        capacities = {
            "a": {"lut": 120, "ff": 10},
            "b": {"lut": 108, "ff": 20},
            "c": {"lut": 120, "ff": 20},
        }
        for fpga in value["fpgas"]:
            fpga["capacity"] = capacities[fpga["id"]]
        platform = Platform.from_dict(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fixed": [{"instance": "u00001", "fpga": "b"}],
                "min_used_fpgas": 3,
                "balance_tolerance": 2.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(ir, constraints)
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        parts = ("a", "b", "c")
        mapping = {
            cluster["id"]: parts[
                int(cluster["instances"][0][1:]) % len(parts)
            ]
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            mapping,
            provider="fixture",
            seed=1,
        )
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 1,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_third_block_ejection",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 3.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 10.0,
            },
            "paths": [
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u00000", "O"),
                    "endpoint": _endpoint("u00001", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 3.0,
                    "fixed_delay_ns": 7.0,
                    "path_nets": ["n00000"],
                    "normalized_slack": 1.0,
                }
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        final, trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
        )
        self.assertEqual(
            trace["mode"], "endpoint-exact-critical-ejection-v6"
        )
        self.assertGreaterEqual(len(trace["moves"]), 1)
        first = trace["moves"][0]
        self.assertEqual(first["kind"], "ejection")
        self.assertEqual(first["cluster"], by_instance["u00000"])
        self.assertEqual(first["source"], "a")
        self.assertEqual(first["target"], "b")
        self.assertEqual(first["partner"], by_instance["u00004"])
        self.assertEqual(first["partner_source"], "b")
        self.assertEqual(first["partner_target"], "c")
        replay = validate_partition_pressure_scalable_trace(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            final,
            trace,
        )
        self.assertEqual(replay["status"], "pass")

    def test_v14_preserves_maximum_path_hop_priority(self) -> None:
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "pressure_static_exact_guard",
                    "top": "pressure_static_exact_guard",
                    "source_format": "fixture",
                },
                "ports": [],
                "instances": [
                    {
                        "id": f"u{index}",
                        "type": "LUT1",
                        "resources": {"lut": 1},
                    }
                    for index in range(4)
                ],
                "nets": [
                    {
                        "id": "architectural-relaxed",
                        "cut_class": "register_output",
                        "drivers": [_endpoint("u0", "O")],
                        "sinks": [_endpoint("u1", "I")],
                    },
                    {
                        "id": "architectural-critical",
                        "cut_class": "register_output",
                        "drivers": [_endpoint("u1", "O")],
                        "sinks": [_endpoint("u2", "I")],
                    },
                ],
                "clocks": [
                    {
                        "id": "clk",
                        "name": "clk",
                        "source_port": "clk",
                        "period_ns": None,
                    }
                ],
                "warnings": [],
            }
        )
        platform = Platform.from_dict(
            _platform_value(
                "pressure_static_exact_guard_platform",
                ["a", "b", "c"],
                [
                    _link("ab", "a", "b", lanes=8, latency=1),
                    _link("bc", "b", "c", lanes=8, latency=1),
                ],
            )
        )
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 3,
                "balance_tolerance": 2.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            static_exact_candidate_policy=(
                STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ),
        )
        by_instance = {
            cluster["instances"][0]: cluster["id"]
            for cluster in clusters["clusters"]
        }
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            {
                by_instance["u0"]: "a",
                by_instance["u1"]: "b",
                by_instance["u2"]: "c",
                by_instance["u3"]: "b",
            },
            provider="fixture",
            seed=1,
        )
        routes = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": 64,
                "tdm_ratio_quantum": 1,
                "max_route_hops": 2,
            },
            platform,
        )
        timing = {
            "schema": "emuflow.sta-path-database/v1",
            "design": "pressure_static_exact_guard",
            "source": {"provider": "fixture", "input": "fixture"},
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 1.0,
                "max_clock_period_ns": 20.0,
            },
            "paths": [
                {
                    "id": "p-relaxed",
                    "startpoint": _endpoint("u0", "O"),
                    "endpoint": _endpoint("u1", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": 100.0,
                    "fixed_delay_ns": 0.0,
                    "path_nets": ["architectural-relaxed"],
                    "normalized_slack": 5.0,
                },
                {
                    "id": "p-critical",
                    "startpoint": _endpoint("u1", "O"),
                    "endpoint": _endpoint("u2", "I"),
                    "clock_domain": "clk",
                    "clock_period_ns": 10.0,
                    "slack_ns": -10.0,
                    "fixed_delay_ns": 20.0,
                    "path_nets": ["architectural-critical"],
                    "normalized_slack": -1.0,
                },
            ],
        }
        model = build_partition_pressure_model(
            ir, platform, clusters, constraints, timing, routes
        )
        unguarded, _ = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
            algorithm_version=9,
        )
        self.assertEqual(
            unguarded["cluster_assignment"][by_instance["u1"]], "b"
        )
        predictive, predictive_trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            routes,
            model,
            initial,
            executable=str(patron_refiner()),
            max_moves=4,
            algorithm_version=14,
        )
        self.assertEqual(
            predictive_trace["mode"], "endpoint-exact-critical-flow-v14"
        )
        self.assertLessEqual(
            predictive_trace["final_metrics"]["maximum_path_hops"],
            predictive_trace["initial_metrics"]["maximum_path_hops"],
        )
        self.assertEqual(
            predictive["cluster_assignment"][by_instance["u1"]], "b"
        )
        self.assertIn(
            "maximum_path_tdm_ratio_sum",
            predictive_trace["final_metrics"],
        )
        self.assertIn(
            "total_path_partition_transitions",
            predictive_trace["initial_metrics"],
        )
        checked = validate_partition_pressure_native_bundle(
            ir,
            platform,
            clusters,
            constraints,
            timing,
            routes,
            model,
            initial,
            predictive,
            predictive_trace,
        )
        self.assertEqual(checked["status"], "pass")

if __name__ == "__main__":
    unittest.main()
