import copy
import hashlib
import io
import json
import os
import random
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import tracemalloc
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import main
from emuflow.combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
    _NativeStaticExactCaptureRequests,
    _NativeStaticExactAnalysisRecords,
    _analyze_static_exact_dependencies,
    _analyze_static_exact_dependencies_native,
    _build_combinational_cut_candidate_index,
    _resolve_native_capture_stream,
    _resolve_native_capture_requests,
    build_static_exact_semantic_contract,
    characterize_combinational_cuts,
    semantic_contract_sha256,
    validate_combinational_cut_characterization,
)
from emuflow.errors import ValidationError
from emuflow.equivalence import (
    _MappedModel,
    exhaustively_verify_static_exact_partition_equivalence,
    simulate_static_exact_partition_equivalence,
)
from emuflow.emuir_compact import write_compact_emuir
from emuflow.ir import EmuIR
from emuflow.io import read_json, write_json
from emuflow.partition_ir import load_partition_ir
from emuflow.netlist import (
    build_split_artifacts,
    transport_to_systemverilog,
    validate_split_artifacts,
)
from emuflow.partition import (
    CUT_MODE_STATIC_EXACT,
    assign_clusters,
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
    validate_partition_artifacts,
)
from emuflow.platform import Platform
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.split_netlist_storage import SplitNetlistView
from emuflow.phase3_assignment_index import (
    ASSIGNMENT_INDEX_APPLICATION_ID,
    migrate_phase3_assignment_index,
    read_phase3_exact_semantic_contract,
    read_phase3_exact_semantic_partition,
    validate_phase3_assignment_index,
    write_phase3_assignment_index,
)
from emuflow.phase3_exact_records import (
    StaticExactCutNodeSequence,
    StaticExactRecordSequence,
    build_static_exact_capture_stream,
    build_static_exact_cut_node_stream,
    read_native_static_exact_cut_node_stream,
    read_native_static_exact_capture_stream,
)
from emuflow.phase3_exact_binary import (
    validate_phase3_exact_binary,
    write_phase3_exact_binary,
)
from emuflow.phase3_storage import (
    PACKED_ASSIGNMENT_SCHEMA,
    pack_phase3_assignment,
    pack_phase3_clusters,
    read_phase3_assignment_view,
)
from emuflow.routing import (
    build_directed_graph,
    demands_from_assignment,
    normalize_route_constraints,
    validate_system_routes,
)
from emuflow.resources import ResourceVector
from emuflow.tdm import (
    TDM_ACADEMIC_LIST_SCHEDULE_PROVIDER,
    TDM_BASELINE_PROVIDER,
    TDM_EXACT_DEPENDENCY_PROVIDER,
    SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
    build_tdm_schedule,
    validate_tdm_schedule,
)
from emuflow.tdm_schedule_storage import (
    read_tdm_schedule,
    write_tdm_schedule_storage,
)
from emuflow.tdm_ratio import build_tdm_ratio_plan
from emuflow.tdm_slot import refine_tdm_schedule_native
from emuflow.timing_routing import route_system_native
from emuflow.transport_storage import write_transport_storage
from emuflow.system_route_storage import (
    SystemRoutesView,
    read_system_routes,
    write_system_route_storage,
)
from tests.native_build import (
    emuflow_native,
    static_exact_analyzer,
    tdm_exact_scheduler,
    tdm_ratio_optimizer,
    tdm_slot_optimizer,
    tlr_router,
)


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


def _endpoint(instance, port):
    return {"instance": instance, "port": port, "bit": 0}


def _chain_ir():
    instances = [
        {"id": "q0", "type": "FDRE", "resources": {"ff": 1}},
        {
            "id": "l0",
            "type": "LUT2",
            "parameters": {"INIT": "1010"},
            "resources": {"lut": 1},
        },
        {
            "id": "l1",
            "type": "LUT2",
            "parameters": {"INIT": "1010"},
            "resources": {"lut": 1},
        },
        {
            "id": "l2",
            "type": "LUT2",
            "parameters": {"INIT": "1010"},
            "resources": {"lut": 1},
        },
        {"id": "q1", "type": "FDRE", "resources": {"ff": 1}},
    ]
    nets = [
        {
            "id": "q",
            "name": "q",
            "cut_class": "register_output",
            "drivers": [_endpoint("q0", "Q")],
            "sinks": [_endpoint("l0", "I0")],
        },
        {
            "id": "n0",
            "name": "n0",
            "cut_class": "combinational",
            "drivers": [_endpoint("l0", "O")],
            "sinks": [_endpoint("l1", "I0")],
        },
        {
            "id": "n1",
            "name": "n1",
            "cut_class": "combinational",
            "drivers": [_endpoint("l1", "O")],
            "sinks": [_endpoint("l2", "I0")],
        },
        {
            "id": "d",
            "name": "d",
            "cut_class": "register_input",
            "drivers": [_endpoint("l2", "O")],
            "sinks": [_endpoint("q1", "D")],
        },
        {
            "id": "clk_net",
            "name": "clk_net",
            "cut_class": "clock",
            "drivers": [_endpoint(None, "clk")],
            "sinks": [_endpoint("q0", "C"), _endpoint("q1", "C")],
        },
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {"name": "cut_chain", "top": "cut_chain", "source_format": "test"},
            "ports": [{"id": "clk", "direction": "input", "width": 1}],
            "instances": instances,
            "nets": nets,
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


def _wide_fanout_ir(width=32):
    instances = [
        {"id": "q0", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "source_lut", "type": "LUT2", "resources": {"lut": 1}},
    ]
    nets = [
        {
            "id": "q",
            "name": "q",
            "cut_class": "register_output",
            "drivers": [_endpoint("q0", "Q")],
            "sinks": [_endpoint("source_lut", "I0")],
        }
    ]
    fanout_sinks = []
    for index in range(width):
        lut = f"sink_lut_{index:03d}"
        register = f"sink_ff_{index:03d}"
        instances.extend(
            [
                {"id": lut, "type": "LUT2", "resources": {"lut": 1}},
                {"id": register, "type": "FDRE", "resources": {"ff": 1}},
            ]
        )
        fanout_sinks.append(_endpoint(lut, "I0"))
        nets.append(
            {
                "id": f"d_{index:03d}",
                "name": f"d_{index:03d}",
                "cut_class": "register_input",
                "drivers": [_endpoint(lut, "O")],
                "sinks": [_endpoint(register, "D")],
            }
        )
    nets.append(
        {
            "id": "wide_boundary",
            "name": "wide_boundary",
            "cut_class": "combinational",
            "drivers": [_endpoint("source_lut", "O")],
            "sinks": fanout_sinks,
        }
    )
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "wide_fanout",
                "top": "wide_fanout",
                "source_format": "test",
            },
            "ports": [],
            "instances": instances,
            "nets": nets,
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


def _reconvergent_ir():
    instances = [
        {"id": "qa", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "qb", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "qc", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "la", "type": "LUT2", "resources": {"lut": 1}},
        {"id": "lb", "type": "LUT2", "resources": {"lut": 1}},
        {
            "id": "merge",
            "type": "LUT3",
            "parameters": {"INIT": "10000000"},
            "resources": {"lut": 1},
        },
        {"id": "after", "type": "LUT2", "resources": {"lut": 1}},
        {"id": "qout", "type": "FDRE", "resources": {"ff": 1}},
    ]
    nets = [
        {
            "id": "qa_q",
            "name": "qa_q",
            "cut_class": "register_output",
            "drivers": [_endpoint("qa", "Q")],
            "sinks": [_endpoint("la", "I0")],
        },
        {
            "id": "qb_q",
            "name": "qb_q",
            "cut_class": "register_output",
            "drivers": [_endpoint("qb", "Q")],
            "sinks": [_endpoint("lb", "I0")],
        },
        {
            "id": "a",
            "name": "a",
            "cut_class": "combinational",
            "drivers": [_endpoint("la", "O")],
            "sinks": [_endpoint("merge", "I0")],
        },
        {
            "id": "b",
            "name": "b",
            "cut_class": "combinational",
            "drivers": [_endpoint("lb", "O")],
            "sinks": [_endpoint("merge", "I1")],
        },
        {
            "id": "qc_q",
            "name": "qc_q",
            "cut_class": "register_output",
            "drivers": [_endpoint("qc", "Q")],
            "sinks": [_endpoint("merge", "I2")],
        },
        {
            "id": "c",
            "name": "c",
            "cut_class": "combinational",
            "drivers": [_endpoint("merge", "O")],
            "sinks": [_endpoint("after", "I0")],
        },
        {
            "id": "d",
            "name": "d",
            "cut_class": "register_input",
            "drivers": [_endpoint("after", "O")],
            "sinks": [_endpoint("qout", "D")],
        },
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "reconvergent_cut",
                "top": "reconvergent_cut",
                "source_format": "test",
            },
            "ports": [],
            "instances": instances,
            "nets": nets,
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


def _three_fpga_platform(topology="line"):
    value = json.loads(PLATFORM_PATH.read_text(encoding="utf-8"))
    value["platform"]["name"] = f"static_exact_three_fpga_{topology}"
    fpga2 = copy.deepcopy(value["fpgas"][1])
    fpga2["id"] = "fpga2"
    value["fpgas"].append(fpga2)
    if topology == "line":
        value["links"].append(
            {
                **copy.deepcopy(value["links"][0]),
                "id": "link_1_2",
                "endpoints": ["fpga1", "fpga2"],
            }
        )
    elif topology == "star":
        value["links"].append(
            {
                **copy.deepcopy(value["links"][0]),
                "id": "link_0_2",
                "endpoints": ["fpga0", "fpga2"],
            }
        )
    else:
        raise AssertionError("unknown fixture topology")
    return Platform.from_dict(value)


class CombinationalCutCharacterizationTest(unittest.TestCase):
    def test_canonical_hash_streams_the_legacy_pretty_json_identity(self):
        value = {
            "z": [1, {"unicode": "π", "enabled": True}],
            "a": {"nested": None},
        }
        expected = hashlib.sha256(
            (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        ).hexdigest()
        with patch(
            "emuflow.combinational_cut.json.dumps",
            side_effect=AssertionError("canonical hashing must stream"),
        ):
            self.assertEqual(semantic_contract_sha256(value), expected)

    def test_mapped_model_evaluates_reverse_named_chain_once_per_cell(self):
        width = 256
        instance_ids = [f"lut_{index:04d}" for index in reversed(range(width))]
        nets = [
            {
                "id": "input_net",
                "name": "input_net",
                "cut_class": "combinational",
                "drivers": [_endpoint(None, "input")],
                "sinks": [_endpoint(instance_ids[0], "I0")],
            }
        ]
        for index, instance_id in enumerate(instance_ids):
            sink = (
                _endpoint(instance_ids[index + 1], "I0")
                if index + 1 < width
                else _endpoint(None, "output")
            )
            nets.append(
                {
                    "id": f"net_{index:04d}",
                    "name": f"net_{index:04d}",
                    "cut_class": "combinational",
                    "drivers": [_endpoint(instance_id, "O")],
                    "sinks": [sink],
                }
            )
        model = _MappedModel(
            EmuIR(
                {
                    "schema": "emuflow.emuir/v1",
                    "design": {
                        "name": "reverse_named_chain",
                        "top": "reverse_named_chain",
                        "source_format": "test",
                    },
                    "ports": [
                        {"id": "input", "direction": "input", "width": 1},
                        {"id": "output", "direction": "output", "width": 1},
                    ],
                    "instances": [
                        {
                            "id": instance_id,
                            "type": "LUT1",
                            "parameters": {"INIT": "10"},
                            "resources": {"lut": 1},
                        }
                        for instance_id in instance_ids
                    ],
                    "nets": nets,
                    "clocks": [],
                    "warnings": [],
                }
            )
        )
        evaluations = []
        original = model._evaluate_combinational_instance

        def counted(values, instance_id, overrides=None):
            evaluations.append(instance_id)
            return original(values, instance_id, overrides)

        model._evaluate_combinational_instance = counted
        values, _, outputs = model.evaluate({}, 0, 17)
        self.assertEqual(evaluations, instance_ids)
        self.assertEqual(len(evaluations), width)
        self.assertIn(f"net_{width - 1:04d}", values)
        self.assertEqual(outputs, {"output[0]": values[f"net_{width - 1:04d}"]})

    def test_mapped_model_advances_only_active_root_clock_domain(self):
        instances = [
            {
                "id": register,
                "type": "FDRE",
                "resources": {"ff": 1},
                "constant_connections": [
                    {"port": "CE", "bit": 0, "value": "1"},
                    {"port": "R", "bit": 0, "value": "0"},
                ],
            }
            for register in ("q_fast", "q_slow")
        ]
        nets = []
        for suffix in ("fast", "slow"):
            nets.extend(
                [
                    {
                        "id": f"clk_{suffix}_net",
                        "name": f"clk_{suffix}_net",
                        "cut_class": "clock",
                        "drivers": [_endpoint(None, f"clk_{suffix}")],
                        "sinks": [_endpoint(f"q_{suffix}", "C")],
                    },
                    {
                        "id": f"d_{suffix}_net",
                        "name": f"d_{suffix}_net",
                        "cut_class": "register_input",
                        "drivers": [_endpoint(None, f"d_{suffix}")],
                        "sinks": [_endpoint(f"q_{suffix}", "D")],
                    },
                    {
                        "id": f"q_{suffix}_net",
                        "name": f"q_{suffix}_net",
                        "cut_class": "register_output",
                        "drivers": [_endpoint(f"q_{suffix}", "Q")],
                        "sinks": [_endpoint(None, f"out_{suffix}")],
                    },
                ]
            )
        model = _MappedModel(
            EmuIR(
                {
                    "schema": "emuflow.emuir/v1",
                    "design": {
                        "name": "two_clock_registers",
                        "top": "two_clock_registers",
                        "source_format": "test",
                    },
                    "ports": [
                        {"id": f"clk_{suffix}", "direction": "input", "width": 1}
                        for suffix in ("fast", "slow")
                    ]
                    + [
                        {"id": f"d_{suffix}", "direction": "input", "width": 1}
                        for suffix in ("fast", "slow")
                    ]
                    + [
                        {"id": f"out_{suffix}", "direction": "output", "width": 1}
                        for suffix in ("fast", "slow")
                    ],
                    "instances": instances,
                    "nets": nets,
                    "clocks": [
                        {
                            "id": f"clk_{suffix}",
                            "name": f"clk_{suffix}",
                            "source_port": f"clk_{suffix}",
                            "period_ns": period,
                        }
                        for suffix, period in (("fast", 10.0), ("slow", 20.0))
                    ],
                    "warnings": [],
                }
            )
        )
        model.validate_clock_domains({"clk_fast": 10.0, "clk_slow": 20.0})
        state = model.initial_state()
        _, state, _ = model.evaluate(
            state,
            cycle=0,
            seed=1,
            input_values={("d_fast", 0): 1, ("d_slow", 0): 1},
            active_clocks={"clk_fast"},
        )
        self.assertEqual(state, {"q_fast": 1, "q_slow": 0})
        _, state, _ = model.evaluate(
            state,
            cycle=1,
            seed=1,
            input_values={("d_fast", 0): 0, ("d_slow", 0): 1},
            active_clocks={"clk_slow"},
        )
        self.assertEqual(state, {"q_fast": 1, "q_slow": 1})

    def test_mapped_model_traces_lut_gated_clock_to_declared_root(self):
        model = _MappedModel(
            EmuIR(
                {
                    "schema": "emuflow.emuir/v1",
                    "design": {
                        "name": "gated_clock",
                        "top": "gated_clock",
                        "source_format": "test",
                    },
                    "ports": [
                        {"id": "clk", "direction": "input", "width": 1},
                        {"id": "enable", "direction": "input", "width": 1},
                        {"id": "d", "direction": "input", "width": 1},
                        {"id": "q", "direction": "output", "width": 1},
                    ],
                    "instances": [
                        {
                            "id": "clock_gate",
                            "type": "LUT2",
                            "parameters": {"INIT": "1000"},
                            "resources": {"lut": 1},
                        },
                        {
                            "id": "register",
                            "type": "FDRE",
                            "resources": {"ff": 1},
                            "constant_connections": [
                                {"port": "CE", "bit": 0, "value": "1"},
                                {"port": "R", "bit": 0, "value": "0"},
                            ],
                        },
                    ],
                    "nets": [
                        {
                            "id": "clk_net",
                            "cut_class": "clock",
                            "drivers": [_endpoint(None, "clk")],
                            "sinks": [_endpoint("clock_gate", "I0")],
                        },
                        {
                            "id": "enable_net",
                            "cut_class": "primary_input",
                            "drivers": [_endpoint(None, "enable")],
                            "sinks": [_endpoint("clock_gate", "I1")],
                        },
                        {
                            "id": "gated_clk_net",
                            "cut_class": "combinational",
                            "drivers": [_endpoint("clock_gate", "O")],
                            "sinks": [_endpoint("register", "C")],
                        },
                        {
                            "id": "d_net",
                            "cut_class": "register_input",
                            "drivers": [_endpoint(None, "d")],
                            "sinks": [_endpoint("register", "D")],
                        },
                        {
                            "id": "q_net",
                            "cut_class": "register_output",
                            "drivers": [_endpoint("register", "Q")],
                            "sinks": [_endpoint(None, "q")],
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
        )

        model.validate_clock_domains({"clk": 10.0})
        self.assertEqual(
            model.sequential_clock_roots["register"], frozenset({"clk"})
        )

    def test_sparse_graph_membership_is_not_quadratic(self):
        class CountingString(str):
            comparisons = 0

            def __eq__(self, other):
                type(self).comparisons += 1
                return super().__eq__(other)

            __hash__ = str.__hash__

        instance_ids = [
            CountingString(f"lut_{index:04d}") for index in range(256)
        ]
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "sparse_membership",
                    "top": "sparse_membership",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {"id": instance_id, "type": "LUT2", "resources": {"lut": 1}}
                    for instance_id in instance_ids
                ],
                "nets": [
                    {
                        "id": CountingString(f"net_{index:04d}"),
                        "name": f"net_{index:04d}",
                        "cut_class": "combinational",
                        "drivers": [_endpoint(instance_id, "O")],
                        "sinks": [],
                    }
                    for index, instance_id in enumerate(instance_ids)
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        CountingString.comparisons = 0
        report = characterize_combinational_cuts(ir, (1,))
        self.assertEqual(report["metrics"]["instances"], len(instance_ids))
        self.assertLess(CountingString.comparisons, len(instance_ids))

    def test_chain_has_stable_dependency_depth_and_split_upper_bounds(self):
        ir = _chain_ir()
        report = characterize_combinational_cuts(ir)
        self.assertFalse(report["behavior_change"])
        cuts = {item["net"]: item for item in report["eligible_cuts"]}
        self.assertEqual(cuts["n0"]["dependency_level"], 1)
        self.assertEqual(cuts["n0"]["predecessor_cut_nets"], [])
        self.assertEqual(cuts["n1"]["dependency_level"], 2)
        self.assertEqual(cuts["n1"]["predecessor_cut_nets"], ["n0"])
        self.assertEqual(
            report["current_sequential_only_atomic_components"]["maximum_instances"],
            3,
        )
        by_limit = {
            item["max_dependency_depth"]: item for item in report["depth_limits"]
        }
        self.assertEqual(by_limit[1]["atomic_components"]["maximum_instances"], 2)
        self.assertEqual(by_limit[2]["atomic_components"]["maximum_instances"], 1)
        self.assertEqual(
            validate_combinational_cut_characterization(ir, report)["status"],
            "pass",
        )

    def test_compact_candidate_index_matches_full_characterization(self):
        ir = _chain_ir()
        report = characterize_combinational_cuts(ir)
        compact = _build_combinational_cut_candidate_index(
            ir,
            include_dependency_levels=True,
            include_source_identity=True,
        )
        self.assertEqual(
            compact["eligible_ids"],
            {item["net"] for item in report["eligible_cuts"]},
        )
        self.assertEqual(
            compact["dependency_levels"],
            {
                item["net"]: item["dependency_level"]
                for item in report["eligible_cuts"]
            },
        )
        self.assertEqual(
            compact["canonical_emuir_sha256"],
            report["source_identity"]["canonical_emuir_sha256"],
        )

    def test_compact_v2_candidate_index_skips_frontier_levels(self):
        ir = _chain_ir()
        compact = _build_combinational_cut_candidate_index(
            ir,
            include_dependency_levels=False,
            include_source_identity=False,
        )
        self.assertEqual(compact["eligible_ids"], {"n0", "n1"})
        self.assertEqual(compact["dependency_levels"], {})
        self.assertNotIn("canonical_emuir_sha256", compact)

    def test_compact_v2_candidate_index_accepts_sparse_endpoint_ordinals(self):
        value = _chain_ir().to_dict()
        expected = _build_combinational_cut_candidate_index(
            EmuIR(value),
            include_dependency_levels=False,
            include_source_identity=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, value)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE endpoint_records SET ordinal=ordinal+1000000"
                )
            actual = _build_combinational_cut_candidate_index(
                load_partition_ir(path),
                include_dependency_levels=False,
                include_source_identity=False,
            )
        self.assertEqual(actual, expected)

    def test_compact_v2_candidate_index_reuses_endpoint_summary(self):
        value = _chain_ir().to_dict()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, value)
            delegate = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            delegate.row_factory = sqlite3.Row
            statements = []

            class RecordingConnection:
                def execute(self, statement, *arguments):
                    statements.append(" ".join(statement.split()))
                    return delegate.execute(statement, *arguments)

                def executemany(self, statement, *arguments):
                    statements.append(" ".join(statement.split()))
                    return delegate.executemany(statement, *arguments)

                def close(self):
                    delegate.close()

            with patch(
                "emuflow.combinational_cut.open_emuir_sqlite_read_only",
                return_value=RecordingConnection(),
            ):
                actual = _build_combinational_cut_candidate_index(
                    load_partition_ir(path),
                    include_dependency_levels=False,
                    include_source_identity=False,
                )
        self.assertEqual(actual["eligible_ids"], {"n0", "n1"})
        endpoint_scans = [
            statement
            for statement in statements
            if "FROM endpoint_records" in statement
        ]
        self.assertEqual(len(endpoint_scans), 1)
        self.assertNotIn("WHERE n.cut_class='combinational'", endpoint_scans[0])
        self.assertNotIn("JOIN pin_names", endpoint_scans[0])
        self.assertNotIn("JOIN cell_records", endpoint_scans[0])
        self.assertFalse(
            any("FROM cell_records GROUP BY id" in item for item in statements)
        )
        self.assertFalse(
            any("FROM net_records GROUP BY id" in item for item in statements)
        )

    def test_compact_v2_candidate_index_streams_many_cyclic_owners(self):
        value = _chain_ir().to_dict()
        value["nets"][2]["sinks"].append(_endpoint("l0", "I1"))
        expected = _build_combinational_cut_candidate_index(
            EmuIR(value),
            include_dependency_levels=False,
            include_source_identity=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, value)
            with patch(
                "emuflow.combinational_cut._COMPACT_CYCLIC_OWNER_INDEX_LIMIT",
                0,
            ):
                actual = _build_combinational_cut_candidate_index(
                    load_partition_ir(path),
                    include_dependency_levels=False,
                    include_source_identity=False,
                )
        self.assertEqual(actual, expected)

    def test_compact_v2_candidate_index_streams_packed_graph_and_matches_reference(
        self,
    ):
        values = []
        values.append(_chain_ir().to_dict())
        cyclic = _chain_ir().to_dict()
        cyclic["nets"][2]["sinks"].append(_endpoint("l0", "I1"))
        values.append(cyclic)
        irregular = _chain_ir().to_dict()
        irregular["nets"][1]["drivers"].append(_endpoint("l1", "O"))
        irregular["nets"][2]["sinks"] = [_endpoint("q1", "CLR")]
        values.append(irregular)

        for index, value in enumerate(values):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                reference = EmuIR(value)
                expected = _build_combinational_cut_candidate_index(
                    reference,
                    include_dependency_levels=False,
                    include_source_identity=False,
                )
                path = Path(temporary) / "design.emuir.sqlite"
                write_compact_emuir(path, value)
                compact_ir = load_partition_ir(path)
                with patch(
                    "emuflow.partition_ir._CompactPartitionInstances.__iter__",
                    side_effect=AssertionError("cell dictionaries were decoded"),
                ), patch(
                    "emuflow.partition_ir._CompactPartitionNets.__iter__",
                    side_effect=AssertionError("net dictionaries were decoded"),
                ), patch(
                    "emuflow.combinational_cut._strongly_connected_components",
                    side_effect=AssertionError("string-keyed SCC was rebuilt"),
                ):
                    actual = _build_combinational_cut_candidate_index(
                        compact_ir,
                        include_dependency_levels=False,
                        include_source_identity=False,
                    )
                packed = actual["eligible_ids"]
                expected_names = sorted(expected["eligible_ids"])
                self.assertEqual(
                    list(packed),
                    expected_names,
                )
                self.assertTrue(hasattr(packed, "contains_ordinal"))
                for net_ordinal, net in enumerate(value["nets"]):
                    self.assertEqual(
                        packed.contains_ordinal(net_ordinal),
                        net["id"] in expected["eligible_ids"],
                    )
                self.assertLessEqual(
                    packed.packed_bytes,
                    (len(value["nets"]) + 7) // 8
                    + sum(
                        len(name.encode("utf-8")) + 8
                        for name in expected["eligible_ids"]
                    )
                    + 8,
                )
                streamed_names = packed.streaming_ordered_names()
                self.assertIsInstance(streamed_names, list)
                self.assertEqual(list.__len__(streamed_names), 0)
                self.assertEqual(len(streamed_names), len(expected_names))
                self.assertEqual(streamed_names, expected_names)
                self.assertEqual(streamed_names[:], expected_names)
                self.assertEqual(
                    json.dumps(streamed_names, separators=(",", ":")),
                    json.dumps(expected_names, separators=(",", ":")),
                )
                copied_names = copy.deepcopy(streamed_names)
                self.assertIs(type(copied_names), list)
                self.assertEqual(copied_names, expected_names)
                streamed_names.append("__compatibility_mutation__")
                self.assertEqual(
                    streamed_names,
                    [*expected_names, "__compatibility_mutation__"],
                )
                self.assertEqual(
                    list(packed), expected_names,
                    "mutating the compatibility view changed packed identity",
                )
                self.assertEqual(actual, expected)

    def test_compact_v2_candidate_index_randomized_differential(self):
        resource_kinds = (
            ("LUT1", {"lut": 1}),
            ("FDRE", {"ff": 1}),
            ("DSP48E2", {"dsp48": 1}),
            ("OPAQUE", {"other": 1}),
        )
        cut_classes = (
            "combinational",
            "combinational",
            "combinational",
            "clock",
            "reset",
            "multi_driver",
            "undriven",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for seed in range(64):
                generator = random.Random(seed)
                instances = []
                for index in range(12):
                    instance_type, resources = generator.choice(resource_kinds)
                    instances.append(
                        {
                            "id": f"u{index}",
                            "type": instance_type,
                            "resources": resources,
                        }
                    )
                nets = []
                for index in range(24):
                    cut_class = generator.choice(cut_classes)
                    driver_count = generator.randrange(3)
                    sink_count = generator.randrange(7)
                    drivers = []
                    sinks = []
                    for _ in range(driver_count):
                        owner = generator.choice(
                            [None, *[item["id"] for item in instances]]
                        )
                        drivers.append(
                            _endpoint(owner, "top_in" if owner is None else "O")
                        )
                    for _ in range(sink_count):
                        owner = generator.choice(
                            [None, *[item["id"] for item in instances]]
                        )
                        if owner is None:
                            port = "top_out"
                        else:
                            instance_type = instances[int(owner[1:])]["type"]
                            port = (
                                generator.choice(("D", "CE", "R", "BAD"))
                                if instance_type == "FDRE"
                                else "I"
                            )
                        sinks.append(_endpoint(owner, port))
                    nets.append(
                        {
                            "id": f"n{index}",
                            "cut_class": cut_class,
                            "drivers": drivers,
                            "sinks": sinks,
                        }
                    )
                value = {
                    "schema": "emuflow.emuir/v1",
                    "design": {
                        "name": f"random_{seed}",
                        "top": "random",
                        "source_format": "test",
                    },
                    "ports": [
                        {"id": "top_in", "direction": "input", "width": 1},
                        {"id": "top_out", "direction": "output", "width": 1},
                    ],
                    "instances": instances,
                    "nets": nets,
                    "clocks": [],
                    "warnings": [],
                }
                expected = _build_combinational_cut_candidate_index(
                    EmuIR(value),
                    include_dependency_levels=False,
                    include_source_identity=False,
                )
                path = root / f"random-{seed}.emuir.sqlite"
                write_compact_emuir(path, value)
                actual = _build_combinational_cut_candidate_index(
                    load_partition_ir(path),
                    include_dependency_levels=False,
                    include_source_identity=False,
                )
                self.assertEqual(actual, expected, f"seed={seed}")

    def test_compact_v2_candidate_index_rejects_corrupt_store(self):
        mutations = {
            "endpoint owner is invalid": lambda connection: connection.execute(
                "UPDATE endpoint_records SET instance_seq=? "
                "WHERE net_seq=0 AND role=0 AND ordinal=0",
                (2**31,),
            ),
            "endpoint role is invalid": lambda connection: connection.execute(
                "UPDATE endpoint_records SET role=2 "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            ),
            "record counts disagree": lambda connection: connection.execute(
                "DELETE FROM endpoint_records "
                "WHERE net_seq=0 AND role=0 AND ordinal=0"
            ),
        }
        for message, mutate in mutations.items():
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "design.emuir.sqlite"
                write_compact_emuir(path, _chain_ir().to_dict())
                compact_ir = load_partition_ir(path)
                with closing(sqlite3.connect(path)) as connection, connection:
                    mutate(connection)
                with self.assertRaisesRegex(ValidationError, message):
                    _build_combinational_cut_candidate_index(
                        compact_ir,
                        include_dependency_levels=False,
                        include_source_identity=False,
                    )

    def test_compact_v2_candidate_index_rejects_invalid_resource_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, _chain_ir().to_dict())
            compact_ir = load_partition_ir(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                resources_seq = connection.execute(
                    "SELECT resources_seq FROM cell_records WHERE seq=0"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE payloads SET value='[1,2,3]' WHERE seq=?",
                    (resources_seq,),
                )
            with self.assertRaisesRegex(
                ValidationError, "instance resources are invalid"
            ):
                _build_combinational_cut_candidate_index(
                    compact_ir,
                    include_dependency_levels=False,
                    include_source_identity=False,
                )

    def test_compact_v2_candidate_index_rejects_validation_bypasses(self):
        def negative_count(connection):
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='record_counts_json'"
            ).fetchone()
            counts = json.loads(row[0])
            counts["endpoint_records"] = -1
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='record_counts_json'",
                (json.dumps(counts, sort_keys=True),),
            )

        def duplicate_cell_identity(connection):
            connection.execute(
                "ALTER TABLE cell_records RENAME TO original_cell_records"
            )
            connection.execute(
                """CREATE TABLE cell_records(
                       seq INTEGER, id TEXT, name TEXT, flags INTEGER,
                       type TEXT, resources_seq INTEGER, parameters_seq INTEGER,
                       attributes_seq INTEGER, constants_seq INTEGER,
                       extras_seq INTEGER)"""
            )
            connection.execute(
                "INSERT INTO cell_records SELECT * FROM original_cell_records"
            )
            duplicate = connection.execute(
                "SELECT id FROM cell_records WHERE seq=0"
            ).fetchone()[0]
            connection.execute(
                "UPDATE cell_records SET id=? WHERE seq=1", (duplicate,)
            )

        mutations = (
            (
                "logical schema is invalid",
                lambda connection: connection.execute(
                    "UPDATE metadata SET value='emuflow.emuir/v999' "
                    "WHERE key='logical_schema'"
                ),
            ),
            (
                "cell type is invalid",
                lambda connection: connection.execute(
                    "UPDATE cell_records SET type='' WHERE seq=0"
                ),
            ),
            (
                "net identity is invalid",
                lambda connection: connection.execute(
                    "UPDATE net_records SET id='' WHERE seq=0"
                ),
            ),
            (
                "endpoint port is invalid",
                lambda connection: connection.execute(
                    "UPDATE pin_names SET name='' WHERE name='O'"
                ),
            ),
            (
                "endpoint bit is invalid",
                lambda connection: connection.execute(
                    "UPDATE endpoint_records SET bit=-1 "
                    "WHERE net_seq=0 AND role=0 AND ordinal=0"
                ),
            ),
            (
                "top-level endpoint is invalid",
                lambda connection: connection.execute(
                    "UPDATE pin_names SET name='missing_top' WHERE name='clk'"
                ),
            ),
            ("record counts disagree", negative_count),
            (
                "net cut class is invalid",
                lambda connection: connection.execute(
                    "UPDATE net_records SET cut_class='wrong-kind' WHERE seq=0"
                ),
            ),
            (
                "net sequence is invalid",
                lambda connection: connection.execute(
                    "UPDATE net_records SET seq=99 WHERE seq=0"
                ),
            ),
            (
                "endpoint sequence is invalid",
                lambda connection: connection.execute(
                    "UPDATE endpoint_records SET ordinal=-1 "
                    "WHERE net_seq=0 AND role=0 AND ordinal=0"
                ),
            ),
            ("cell identity is duplicated", duplicate_cell_identity),
            (
                "candidate store is invalid",
                lambda connection: connection.execute("DROP TABLE pin_names"),
            ),
        )
        for message, mutate in mutations:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "design.emuir.sqlite"
                write_compact_emuir(path, _chain_ir().to_dict())
                compact_ir = load_partition_ir(path)
                with closing(sqlite3.connect(path)) as connection, connection:
                    mutate(connection)
                with self.assertRaisesRegex(ValidationError, message):
                    _build_combinational_cut_candidate_index(
                        compact_ir,
                        include_dependency_levels=False,
                        include_source_identity=False,
                    )

    def test_compact_v2_empty_connectivity_net_is_not_eligible(self):
        value = _chain_ir().to_dict()
        value["nets"].append(
            {
                "id": "empty_connectivity",
                "cut_class": "combinational",
                "drivers": [],
                "sinks": [],
            }
        )
        reference = EmuIR(value)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, value)
            compact = _build_combinational_cut_candidate_index(
                load_partition_ir(path),
                include_dependency_levels=False,
                include_source_identity=False,
            )
        expected = _build_combinational_cut_candidate_index(
            reference,
            include_dependency_levels=False,
            include_source_identity=False,
        )
        self.assertEqual(compact, expected)
        self.assertNotIn("empty_connectivity", compact["eligible_ids"])

    def test_compact_v2_million_fanout_endpoints_have_bounded_python_peak(self):
        sink_count = 250_000
        value = {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "million_fanout",
                "top": "million_fanout",
                "source_format": "test",
            },
            "ports": [
                {"id": "clk", "direction": "input", "width": 1},
                {"id": "rst", "direction": "input", "width": 1},
            ],
            "instances": [
                {"id": "source", "type": "LUT1", "resources": {"lut": 1}},
                {
                    "id": "sink_000001",
                    "type": "LUT1",
                    "resources": {"lut": 1},
                },
            ],
            "nets": [
                {
                    "id": "clock_a",
                    "cut_class": "clock",
                    "drivers": [_endpoint(None, "clk")],
                    "sinks": [_endpoint("sink_000001", "C")],
                },
                {
                    "id": "reset_a",
                    "cut_class": "reset",
                    "drivers": [_endpoint(None, "rst")],
                    "sinks": [_endpoint("sink_000001", "R")],
                },
                {
                    "id": "clock_b",
                    "cut_class": "clock",
                    "drivers": [_endpoint(None, "clk")],
                    "sinks": [_endpoint("sink_000001", "CE")],
                },
                {
                    "id": "eligible_fanout",
                    "cut_class": "combinational",
                    "drivers": [_endpoint("source", "O")],
                    "sinks": [_endpoint("sink_000001", "I0")],
                },
            ],
            "clocks": [],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "million.emuir.sqlite"
            write_compact_emuir(path, value)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("PRAGMA synchronous=OFF")
                template = connection.execute(
                    """SELECT resources_seq,constants_seq,extras_seq
                         FROM cell_records WHERE seq=1"""
                ).fetchone()
                connection.executemany(
                    """INSERT INTO cell_records
                       (seq,id,name,flags,type,resources_seq,parameters_seq,
                        attributes_seq,constants_seq,extras_seq)
                       VALUES (?,?,NULL,0,'LUT1',?,NULL,NULL,?,?)""",
                    (
                        (
                            sequence,
                            f"sink_{sequence:06d}",
                            template[0],
                            template[1],
                            template[2],
                        )
                        for sequence in range(2, sink_count + 1)
                    ),
                )
                net_rows = list(
                    connection.execute(
                        "SELECT seq,id FROM net_records ORDER BY seq"
                    )
                )
                pin_by_name = dict(
                    connection.execute("SELECT name,seq FROM pin_names")
                )
                empty_payload = connection.execute(
                    "SELECT seq FROM payloads WHERE value='{}'"
                ).fetchone()[0]
                sink_port = {
                    "clock_a": "C",
                    "reset_a": "R",
                    "clock_b": "CE",
                    "eligible_fanout": "I0",
                }
                for net_seq, net_id in net_rows:
                    port_seq = pin_by_name[sink_port[net_id]]
                    connection.executemany(
                        "INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)",
                        (
                            (
                                net_seq,
                                1,
                                ordinal - 1,
                                ordinal,
                                port_seq,
                                0,
                                empty_payload,
                            )
                            for ordinal in range(2, sink_count + 1)
                        ),
                    )
                counts_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='record_counts_json'"
                ).fetchone()
                counts = json.loads(counts_row[0])
                counts["cell_records"] = sink_count + 1
                counts["endpoint_records"] += 4 * (sink_count - 1)
                connection.execute(
                    "UPDATE metadata SET value=? WHERE key='record_counts_json'",
                    (json.dumps(counts, separators=(",", ":"), sort_keys=True),),
                )

            compact_ir = load_partition_ir(path)
            tracemalloc.start()
            with patch(
                "emuflow.combinational_cut.ResourceVector.from_mapping",
                wraps=ResourceVector.from_mapping,
            ) as decode:
                result = _build_combinational_cut_candidate_index(
                    compact_ir,
                    include_dependency_levels=False,
                    include_source_identity=False,
                )
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        self.assertEqual(result["eligible_ids"], {"eligible_fanout"})
        self.assertEqual(decode.call_count, 1)
        self.assertLess(peak, 16 * 1024 * 1024)

    def test_cycle_is_fail_closed_and_stays_atomic(self):
        value = _chain_ir().to_dict()
        value["nets"][2]["sinks"].append(_endpoint("l0", "I1"))
        ir = EmuIR(value)
        report = characterize_combinational_cuts(ir)
        self.assertEqual(report["metrics"]["cyclic_combinational_sccs"], 1)
        self.assertEqual(report["eligible_cuts"], [])
        reasons = {
            item["net"]: item["reasons"]
            for item in report["ineligible_combinational_cuts"]
        }
        self.assertIn("driver-in-combinational-cycle", reasons["n0"])
        self.assertIn("sink-in-combinational-cycle", reasons["n1"])

    def test_opaque_driver_is_not_eligible(self):
        value = _chain_ir().to_dict()
        instance = next(item for item in value["instances"] if item["id"] == "l0")
        instance["type"] = "VTR_MULTIPLY"
        instance["resources"] = {"dsp": 1}
        report = characterize_combinational_cuts(EmuIR(value))
        reasons = {
            item["net"]: item["reasons"]
            for item in report["ineligible_combinational_cuts"]
        }
        self.assertIn("driver-not-supported-soft-logic", reasons["n0"])

    def test_multi_driver_async_control_and_latch_sinks_fail_closed(self):
        value = _chain_ir().to_dict()
        value["nets"][1]["drivers"].append(_endpoint("l1", "O"))
        value["nets"][2]["sinks"] = [_endpoint("q1", "CLR")]
        value["instances"].append(
            {"id": "lat", "type": "DLATCH", "resources": {"ff": 1}}
        )
        value["nets"].append(
            {
                "id": "latch_control",
                "name": "latch_control",
                "cut_class": "combinational",
                "drivers": [_endpoint("l2", "O")],
                "sinks": [_endpoint("lat", "G")],
            }
        )
        report = characterize_combinational_cuts(EmuIR(value))
        reasons = {
            item["net"]: item["reasons"]
            for item in report["ineligible_combinational_cuts"]
        }
        self.assertIn("not-single-instance-driver", reasons["n0"])
        self.assertIn(
            "unsupported-sequential-control-or-memory-sink", reasons["n1"]
        )
        self.assertIn(
            "unsupported-sequential-control-or-memory-sink",
            reasons["latch_control"],
        )

    def test_tampered_report_is_rejected(self):
        ir = _chain_ir()
        report = characterize_combinational_cuts(ir)
        tampered = copy.deepcopy(report)
        tampered["eligible_cuts"][1]["dependency_level"] = 1
        with self.assertRaisesRegex(ValidationError, "independent EmuIR"):
            validate_combinational_cut_characterization(ir, tampered)

    def test_invalid_depth_limit_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "positive integers"):
            characterize_combinational_cuts(_chain_ir(), (0,))

    def test_characterization_supports_arbitrary_positive_depth(self):
        report = characterize_combinational_cuts(_chain_ir(), (3,))
        self.assertEqual(
            report["depth_limits"][0]["max_dependency_depth"], 3
        )

    def test_cli_writes_and_independently_validates_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            report_path = root / "characterization.json"
            ir_path.write_text(
                json.dumps(_chain_ir().to_dict()), encoding="utf-8"
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "combinational-cut",
                            "characterize",
                            "--ir",
                            str(ir_path),
                            "--depth-limit",
                            "1",
                            "--depth-limit",
                            "2",
                            "--output",
                            str(report_path),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "combinational-cut",
                            "validate",
                            "--ir",
                            str(ir_path),
                            str(report_path),
                        ]
                    ),
                    0,
                )
            self.assertTrue(report_path.is_file())


class StaticExactCombinationalCutPartitionTest(unittest.TestCase):
    def setUp(self):
        self.ir = _chain_ir()
        self.platform = Platform.load(PLATFORM_PATH)
        self.constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )

    def test_native_semantic_analysis_matches_reference(self):
        for dependent_return in (False, True):
            _, assignment = self._exact_artifacts(
                dependent_return=dependent_return
            )
            expected = assignment["semantic_contract"]
            executable = static_exact_analyzer()
            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
            ), patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                0,
            ):
                actual = build_static_exact_semantic_contract(
                    self.ir,
                    self.platform.to_dict(),
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=expected[
                        "max_cross_fpga_dependency_depth"
                    ],
                )
                with patch(
                    "emuflow.combinational_cut.build_static_exact_cut_node_stream",
                    side_effect=AssertionError("native records must not be repacked in Python"),
                ):
                    streamed = build_static_exact_semantic_contract(
                        self.ir,
                        self.platform.to_dict(),
                        assignment["instance_assignment"],
                        assignment["cut_nets"],
                        max_dependency_depth=expected[
                            "max_cross_fpga_dependency_depth"
                        ],
                        stream_records=True,
                    )
                self.assertTrue(streamed["cut_nodes"].native_semantic_artifact_path.is_file())
            self.assertEqual(actual, expected)
            self.assertIsInstance(
                streamed["cut_nodes"], StaticExactCutNodeSequence
            )
            self.assertIsNotNone(
                streamed["cut_nodes"].direct_binary_source
            )
            self.assertEqual(list(streamed["cut_nodes"]), expected["cut_nodes"])
            self.assertEqual(
                semantic_contract_sha256(streamed),
                semantic_contract_sha256(expected),
            )
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                expected_path = root / "expected.bin"
                streamed_path = root / "streamed.bin"
                expected_descriptor = write_phase3_exact_binary(
                    expected_path, expected, ["fpga0", "fpga1"]
                )
                streamed_descriptor = write_phase3_exact_binary(
                    streamed_path,
                    streamed,
                    ["fpga0", "fpga1"],
                    semantic_digest=semantic_contract_sha256(streamed),
                )
                self.assertEqual(
                    streamed_path.read_bytes(), expected_path.read_bytes()
                )
                self.assertEqual(streamed_descriptor, expected_descriptor)
                self.assertEqual(
                    validate_phase3_exact_binary(
                        streamed_path, streamed_descriptor
                    )["status"],
                    "pass",
                )

    def test_native_semantic_analysis_accepts_noncanonical_emuir_order(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        executable = static_exact_analyzer()
        for seed in range(5):
            shuffled_value = copy.deepcopy(self.ir.value)
            generator = random.Random(seed)
            generator.shuffle(shuffled_value["instances"])
            generator.shuffle(shuffled_value["nets"])
            for net in shuffled_value["nets"]:
                generator.shuffle(net["drivers"])
                generator.shuffle(net["sinks"])
            shuffled = EmuIR(shuffled_value)

            with patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                2**63,
            ):
                expected = build_static_exact_semantic_contract(
                    shuffled,
                    self.platform.to_dict(),
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=assignment["semantic_contract"][
                        "max_cross_fpga_dependency_depth"
                    ],
                )
            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
            ), patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                0,
            ):
                actual = build_static_exact_semantic_contract(
                    shuffled,
                    self.platform.to_dict(),
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=assignment["semantic_contract"][
                        "max_cross_fpga_dependency_depth"
                    ],
                )

            self.assertEqual(actual, expected)
            self.assertEqual(
                semantic_contract_sha256(actual),
                semantic_contract_sha256(expected),
            )

    def test_native_v3_capture_coordinates_do_not_use_legacy_full_replay(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ), patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            0,
        ), patch(
            "emuflow.combinational_cut._resolve_native_capture_stream",
            side_effect=AssertionError(
                "protocol v3 must not invoke the legacy endpoint replay"
            ),
        ):
            actual = build_static_exact_semantic_contract(
                self.ir,
                self.platform.to_dict(),
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=assignment["semantic_contract"][
                    "max_cross_fpga_dependency_depth"
                ],
            )

        self.assertEqual(actual, assignment["semantic_contract"])

    def test_compact_native_path_uses_integer_store_without_net_random_access(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        expected = assignment["semantic_contract"]
        ordinal_by_net = {
            net["id"]: index for index, net in enumerate(self.ir.value["nets"])
        }
        ordinals = [
            ordinal_by_net[cut["net"]] for cut in assignment["cut_nets"]
        ]
        executable = static_exact_analyzer()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, self.ir.to_dict())
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE endpoint_records SET ordinal=ordinal+100 "
                    "WHERE net_seq=0 AND role=0"
                )
            compact_ir = load_partition_ir(path)
            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
            ), patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                0,
            ), patch(
                "emuflow.partition_ir._CompactPartitionNets.__getitem__",
                side_effect=AssertionError(
                    "compact native bridge must not randomly decode nets"
                ),
            ), patch(
                "emuflow.combinational_cut."
                "_resolve_native_coordinate_capture_stream",
                side_effect=AssertionError(
                    "compact captures must use the indexed ordered merge"
                ),
            ):
                actual = build_static_exact_semantic_contract(
                    compact_ir,
                    self.platform.to_dict(),
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=expected[
                        "max_cross_fpga_dependency_depth"
                    ],
                    cut_net_ordinals=ordinals,
                )

                with patch(
                    "emuflow.combinational_cut._normalize_native_capture_stream",
                    side_effect=AssertionError("compact production must not normalize captures in Python"),
                ):
                    streamed = build_static_exact_semantic_contract(
                        compact_ir, self.platform.to_dict(), assignment["instance_assignment"],
                        assignment["cut_nets"], max_dependency_depth=expected["max_cross_fpga_dependency_depth"],
                        cut_net_ordinals=ordinals, stream_records=True,
                    )
                capture_path = streamed["capture_requirements"].native_capture_artifact_path
                self.assertTrue(capture_path.is_file())
                self.assertEqual([path.name for path in capture_path.parent.iterdir()], ["captures.bin"])
                self.assertEqual(list(streamed["capture_requirements"]), expected["capture_requirements"])
                expected_bin, actual_bin = Path(temporary) / "expected.bin", Path(temporary) / "actual.bin"
                write_phase3_exact_binary(expected_bin, expected, ["fpga0", "fpga1"])
                write_phase3_exact_binary(actual_bin, streamed, ["fpga0", "fpga1"])
                self.assertEqual(expected_bin.read_bytes(), actual_bin.read_bytes())

        self.assertEqual(actual, expected)
        self.assertEqual(
            semantic_contract_sha256(actual),
            semantic_contract_sha256(expected),
        )

    def test_native_indexed_import_matches_legacy_and_rejects_corruption(self):
        # Exercise the native boundary itself; parent validation must not mask
        # bad schema, coverage, endpoints, or an incomplete partition column.
        _, assignment = self._exact_artifacts(dependent_return=True)
        instances, nets = self.ir.value["instances"], self.ir.value["nets"]
        instance_ids = {item["id"]: index for index, item in enumerate(instances)}
        net_ids = {item["id"]: index for index, item in enumerate(nets)}
        parts = sorted(set(assignment["instance_assignment"].values()))
        cuts = sorted(assignment["cut_nets"], key=lambda cut: cut["net"])
        executable = static_exact_analyzer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "design.sqlite"
            write_compact_emuir(database, self.ir.to_dict())
            legacy = bytearray(struct.pack(
                "<Q6I", 0x31584553464D5545, 3, len(instances), len(nets),
                len(cuts), len(parts), sum(len(net["sinks"]) for net in nets),
            ))
            indexed = bytearray(legacy)
            struct.pack_into("<I", indexed, 8, 4)
            for instance in instances:
                part = parts.index(assignment["instance_assignment"][instance["id"]])
                state = any(instance["resources"].get(key, 0)
                            for key in ("ff", "bram", "bram18k", "uram288"))
                legacy.extend(struct.pack("<BI", state, part))
                indexed.extend(struct.pack("<I", part))
            for net in nets:
                for role in ("drivers", "sinks"):
                    legacy.extend(struct.pack("<I", len(net[role])))
                    for endpoint in net[role]:
                        legacy.extend(struct.pack("<i", instance_ids.get(endpoint["instance"], -1)))
            for cut in cuts:
                sinks = sorted(set(cut["sink_fpgas"]))
                row = struct.pack("<3I", net_ids[cut["net"]],
                                  parts.index(cut["source_fpgas"][0]), len(sinks))
                row += b"".join(struct.pack("<I", parts.index(sink)) for sink in sinks)
                legacy.extend(row)
                indexed.extend(row)
            legacy_input, indexed_input = root / "legacy.in", root / "indexed.in"
            legacy_input.write_bytes(legacy)
            indexed_input.write_bytes(indexed)
            legacy_output, indexed_output = root / "legacy.out", root / "indexed.out"
            subprocess.run([str(executable), str(legacy_input), str(legacy_output)], check=True)
            source_digest = hashlib.sha256(database.read_bytes()).hexdigest()
            command = [str(executable), "--indexed-input", str(database),
                       str(indexed_input), str(indexed_output)]
            subprocess.run(command, check=True)
            self.assertEqual(indexed_output.read_bytes(), legacy_output.read_bytes())
            subprocess.run([str(emuflow_native()), "phase3", "analyze",
                            "--indexed-input", str(database), str(indexed_input),
                            str(indexed_output)], check=True)
            self.assertEqual(indexed_output.read_bytes(), legacy_output.read_bytes())
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), source_digest)
            # Native capture normalization must terminate at the exact record
            # count, remain byte/record-equivalent, and reject damaged input.
            parts_path, captures_path = root / "parts", root / "captures.bin"
            parts_path.write_bytes(struct.pack("<QII", 0x31504553464D5545, 1, len(parts)) +
                                   b"".join(struct.pack("<I", len(part.encode())) + part.encode() for part in parts))
            raw_topology = indexed_output.read_bytes()
            offset = 24
            for _ in cuts:
                _, edges = struct.unpack_from("<BI", raw_topology, offset)
                offset += 5 + 4 * edges
            def capture_command(output):
                return [str(emuflow_native()), "phase3", "resolve-captures", str(database),
                        str(indexed_input), str(indexed_output), str(parts_path), str(output)]
            subprocess.run(capture_command(captures_path), check=True, timeout=15)
            from emuflow.combinational_cut import _resolve_compact_native_coordinate_capture_stream
            reference_captures = _resolve_compact_native_coordinate_capture_stream(
                database, struct.iter_unpack("<4I", raw_topology[offset:]),
                [cut["net"] for cut in cuts], parts, cuts, assignment["instance_assignment"],
                root / "reference-captures.sqlite",
            )
            native_captures = read_native_static_exact_capture_stream(captures_path)
            self.assertEqual(list(native_captures), list(reference_captures))
            del native_captures  # Release the mapped fixture before retirement.
            capture_bytes = captures_path.read_bytes()
            for number, payload in enumerate((capture_bytes[:16], capture_bytes + b"extra",
                    capture_bytes[:24] + struct.pack("<Q", 1) + capture_bytes[32:])):
                corrupted = root / f"corrupt-captures-{number}"
                corrupted.write_bytes(payload)
                with self.assertRaises(ValidationError):
                    list(read_native_static_exact_capture_stream(corrupted))
            # Repeated physical endpoints must collapse to one canonical fact;
            # non-ASCII ports and the largest compact integer range stay exact.
            source_before = database.read_bytes()
            request_before = indexed_input.read_bytes()
            coordinates = list(struct.iter_unpack("<4I", raw_topology[offset:]))
            self.assertTrue(coordinates)
            _, _, capture_net, capture_sink = coordinates[0]
            with closing(sqlite3.connect(database)) as connection, connection:
                next_sink = connection.execute(
                    "SELECT max(ordinal)+1 FROM endpoint_records WHERE net_seq=? AND role=1",
                    (capture_net,),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO endpoint_records SELECT net_seq,role,?,instance_seq,port_seq,bit,extras_seq "
                    "FROM endpoint_records WHERE net_seq=? AND role=1 AND ordinal=?",
                    (next_sink, capture_net, capture_sink),
                )
                connection.execute("UPDATE pin_names SET name=name || '端口α'")
                connection.execute("UPDATE endpoint_records SET bit=?", (2**63-1,))
                counts = json.loads(connection.execute(
                    "SELECT value FROM metadata WHERE key='record_counts_json'"
                ).fetchone()[0])
                counts["endpoint_records"] += 1
                connection.execute("UPDATE metadata SET value=? WHERE key='record_counts_json'", (json.dumps(counts),))
            coordinates += [(cut, part, net, next_sink) for cut, part, net, sink in coordinates
                            if (net, sink) == (capture_net, capture_sink)]
            coordinates.sort(key=lambda row: (row[2], row[3], row[0], row[1]))
            duplicate_topology = bytearray(raw_topology[:offset])
            struct.pack_into("<Q", duplicate_topology, 16, len(coordinates))
            duplicate_topology.extend(b"".join(struct.pack("<4I", *row) for row in coordinates))
            indexed_output.write_bytes(duplicate_topology)
            duplicate_request = bytearray(request_before)
            struct.pack_into("<I", duplicate_request, 28, struct.unpack_from("<I", duplicate_request, 28)[0] + 1)
            indexed_input.write_bytes(duplicate_request)
            repeated_output = root / "repeated-captures.bin"
            subprocess.run(capture_command(repeated_output), check=True, timeout=15)
            repeated_reference = _resolve_compact_native_coordinate_capture_stream(
                database, iter(coordinates), [cut["net"] for cut in cuts], parts, cuts,
                assignment["instance_assignment"], root / "repeated-reference.sqlite",
            )
            repeated_native = read_native_static_exact_capture_stream(repeated_output)
            self.assertEqual(list(repeated_native), list(repeated_reference))
            self.assertLess(len(repeated_native), len(coordinates))
            del repeated_native
            database.write_bytes(source_before)
            indexed_input.write_bytes(request_before)
            indexed_output.write_bytes(raw_topology)
            valid_request, valid_parts = indexed_input.read_bytes(), parts_path.read_bytes()
            malformed_coordinate = bytearray(raw_topology)
            if offset < len(raw_topology):
                struct.pack_into("<I", malformed_coordinate, offset, len(cuts))
            for number, (request_data, topology_data, part_data) in enumerate((
                (valid_request[:-1], raw_topology, valid_parts),
                (valid_request, raw_topology[:-1], valid_parts),
                (valid_request, raw_topology + b"extra", valid_parts),
                (valid_request, malformed_coordinate, valid_parts),
                (valid_request, raw_topology, valid_parts[:-1]),
            )):
                indexed_input.write_bytes(request_data)
                indexed_output.write_bytes(topology_data)
                parts_path.write_bytes(part_data)
                rejected = root / f"rejected-captures-{number}"
                rejected.write_bytes(b"previous-generation")
                result = subprocess.run(capture_command(rejected), capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(rejected.read_bytes(), b"previous-generation")
            indexed_input.write_bytes(valid_request)
            indexed_output.write_bytes(raw_topology)
            parts_path.write_bytes(valid_parts)
            pristine = database.read_bytes()
            for mutation in (
                "PRAGMA user_version=99",
                "UPDATE cell_records SET resources_seq=1000000 WHERE seq=0",
                "UPDATE endpoint_records SET instance_seq=-1 WHERE net_seq=0",
                "UPDATE endpoint_records SET net_seq=net_seq+1000000",
                "UPDATE metadata SET value='{}' WHERE key='record_counts_json'",
            ):
                with self.subTest(mutation=mutation):
                    database.write_bytes(pristine)
                    with closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute(mutation)
                    indexed_output.write_bytes(b"previous-generation")
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(indexed_output.read_bytes(), b"previous-generation")
            database.write_bytes(pristine)
            for payload in (indexed[:32], indexed[:-1], indexed + b"trailing"):
                with self.subTest(size=len(payload)):
                    indexed_input.write_bytes(payload)
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(indexed_output.read_bytes(), b"previous-generation")

    def test_native_semantic_producer_rejects_old_private_record_version(self):
        from emuflow.combinational_cut import _build_native_semantic_cut_nodes

        topology = _NativeStaticExactAnalysisRecords(0)
        topology.native_topology_path = Path("unused-topology")
        def old_producer(command, **kwargs):
            Path(command[-1]).write_bytes(struct.pack("<QIIQQ", 0x314E4553464D5545, 1, 0, 0, 0))
            return subprocess.CompletedProcess(command, 0, "", "")
        from emuflow.phase3_exact_records import read_native_static_exact_cut_node_stream
        held = []
        def reader(path):
            nodes = read_native_static_exact_cut_node_stream(path)
            held.append((nodes, nodes._native_mapping[0]))
            return nodes
        with patch("emuflow.combinational_cut.resolve_native_stage_command", return_value=["native"]), patch(
            "emuflow.combinational_cut.subprocess.run", side_effect=old_producer
        ), patch("emuflow.combinational_cut.read_native_static_exact_cut_node_stream", side_effect=reader), self.assertRaisesRegex(
            ValidationError, "record format v2"
        ):
            _build_native_semantic_cut_nodes([], topology, build_static_exact_capture_stream([]))
        # Retain both the result and its mmap beyond the rejected operation:
        # NFS retirement must not depend on traceback/GC releasing either.
        self.assertEqual(len(held), 1)
        self.assertIsNone(held[0][0]._native_mapping)
        self.assertTrue(held[0][1].closed)

    def test_native_semantic_finalizer_matches_records_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            topology, metadata, output = root / "topology", root / "metadata", root / "nodes"
            valid_topology = struct.pack("<QIIQ", 0x315845534F4D5545, 3, 2, 0)
            valid_topology += struct.pack("<BI", 1, 0) + struct.pack("<BII", 0, 1, 0)
            valid_metadata = bytearray(struct.pack("<QII", 0x314D4553464D5545, 1, 2))
            for net, source, sink in (("a", "fpga0", "fpga1"), ("b", "fpga1", "fpga0")):
                for value in (net, "combinational", source):
                    raw = value.encode()
                    valid_metadata.extend(struct.pack("<I", len(raw)) + raw)
                raw = sink.encode()
                valid_metadata.extend(struct.pack("<II", 1, len(raw)) + raw + struct.pack("<I", 0))
            topology.write_bytes(valid_topology)
            metadata.write_bytes(valid_metadata)
            command = [str(emuflow_native()), "phase3", "finalize-semantics",
                       str(topology), str(metadata), str(output)]
            subprocess.run(command, check=True)
            native = read_native_static_exact_cut_node_stream(output)
            expected = []
            for index, (net, source, sink) in enumerate((("a", "fpga0", "fpga1"), ("b", "fpga1", "fpga0"))):
                expected.append({
                    "net": net, "cut_class": "combinational",
                    "source_fpgas": [source], "sink_fpgas": [sink],
                    "dependency_level": index + 1,
                    "combinational_dependency_depth": index + 1,
                    "predecessor_cut_nets": [] if index == 0 else ["a"],
                    "source_segment_ids": [f"segment{index:06d}"],
                    "capture_segment_ids": [],
                })
            self.assertEqual(list(native), expected)
            reference = build_static_exact_cut_node_stream(expected)
            self.assertEqual([native.binary_node(i) for i in range(2)],
                             [reference.binary_node(i) for i in range(2)])
            valid_output = output.read_bytes()
            for payload in (valid_output[:16], valid_output + b"extra",
                            valid_output[:24] + struct.pack("<Q", 1) + valid_output[32:]):
                corrupt = root / "corrupt"
                corrupt.write_bytes(payload)
                with self.assertRaises(ValidationError):
                    read_native_static_exact_cut_node_stream(corrupt)
            # Close the mapped generation by releasing its only reader before
            # exercising replacement of the output path.
            del native
            for bad_topology, bad_metadata in (
                (valid_topology[:-1], valid_metadata),
                (valid_topology + b"extra", valid_metadata),
                (valid_topology, valid_metadata[:-1]),
                (valid_topology, valid_metadata + b"extra"),
                (struct.pack("<QIIQ", 0x315845534F4D5545, 3, 2, 0)
                 + struct.pack("<BII", 0, 1, 1) + struct.pack("<BII", 0, 1, 0), valid_metadata),
            ):
                topology.write_bytes(bad_topology)
                metadata.write_bytes(bad_metadata)
                output.write_bytes(b"previous-generation")
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), b"previous-generation")
    def test_compact_native_path_accepts_sparse_endpoint_ordinals(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        expected = assignment["semantic_contract"]
        ordinal_by_net = {
            net["id"]: index for index, net in enumerate(self.ir.value["nets"])
        }
        ordinals = [
            ordinal_by_net[cut["net"]] for cut in assignment["cut_nets"]
        ]
        executable = static_exact_analyzer()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, self.ir.to_dict())
            connection = sqlite3.connect(path)
            try:
                # Retain endpoint order while reproducing a filtered compact
                # artifact whose stored sink ordinals have gaps and a nonzero
                # first value. Logical EmuIR iteration makes these positions
                # dense, so the native bridge must do the same.
                connection.execute(
                    "UPDATE endpoint_records SET ordinal=ordinal+1000 "
                    "WHERE role=1"
                )
                connection.execute(
                    "UPDATE endpoint_records SET ordinal=(ordinal-1000)*4+1 "
                    "WHERE role=1"
                )
                connection.commit()
            finally:
                connection.close()
            compact_ir = load_partition_ir(path)
            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
            ), patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                0,
            ):
                actual = build_static_exact_semantic_contract(
                    compact_ir,
                    self.platform.to_dict(),
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=expected[
                        "max_cross_fpga_dependency_depth"
                    ],
                    cut_net_ordinals=ordinals,
                )

        self.assertEqual(actual, expected)
        self.assertEqual(
            semantic_contract_sha256(actual),
            semantic_contract_sha256(expected),
        )

    def test_compact_native_path_rejects_forged_cut_ordinal(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        ordinal_by_net = {
            net["id"]: index for index, net in enumerate(self.ir.value["nets"])
        }
        ordinals = [
            ordinal_by_net[cut["net"]] for cut in assignment["cut_nets"]
        ]
        ordinals[0] = (ordinals[0] + 1) % len(self.ir.value["nets"])
        executable = static_exact_analyzer()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, self.ir.to_dict())
            compact_ir = load_partition_ir(path)
            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
            ), patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                0,
            ), self.assertRaisesRegex(
                ValidationError, "net ordinals are invalid"
            ):
                build_static_exact_semantic_contract(
                    compact_ir,
                    self.platform.to_dict(),
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=assignment["semantic_contract"][
                        "max_cross_fpga_dependency_depth"
                    ],
                    cut_net_ordinals=ordinals,
                )

    def test_compact_native_path_rejects_incomplete_assignment(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        ordinal_by_net = {
            net["id"]: index for index, net in enumerate(self.ir.value["nets"])
        }
        ordinals = [
            ordinal_by_net[cut["net"]] for cut in assignment["cut_nets"]
        ]
        incomplete = dict(assignment["instance_assignment"])
        incomplete.pop(next(iter(incomplete)))
        executable = static_exact_analyzer()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.emuir.sqlite"
            write_compact_emuir(path, self.ir.to_dict())
            compact_ir = load_partition_ir(path)
            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
            ), patch(
                "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
                0,
            ), self.assertRaisesRegex(
                ValidationError, "assignment is incomplete"
            ):
                build_static_exact_semantic_contract(
                    compact_ir,
                    self.platform.to_dict(),
                    incomplete,
                    assignment["cut_nets"],
                    max_dependency_depth=assignment["semantic_contract"][
                        "max_cross_fpga_dependency_depth"
                    ],
                    cut_net_ordinals=ordinals,
                )

    def test_partition_build_carries_cut_ordinals_without_name_lookup(self):
        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ), patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            0,
        ), patch(
            "emuflow.combinational_cut."
            "_resolve_legacy_static_exact_cut_net_ordinals",
            side_effect=AssertionError(
                "new Phase 3 construction must carry authoritative ordinals"
            ),
        ):
            clusters, assignment = self._exact_artifacts(dependent_return=True)
            validation = validate_partition_artifacts(
                self.ir, self.platform, clusters, assignment
            )

        self.assertEqual(validation["status"], "pass")
        self.assertEqual(
            assignment["semantic_contract"]["metrics"]["transported_cut_nets"],
            len(assignment["cut_nets"]),
        )

    def test_native_semantic_analysis_rejects_forged_cut_ordinal(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        net_ordinal = {
            net["id"]: index for index, net in enumerate(self.ir.value["nets"])
        }
        ordinals = [net_ordinal[cut["net"]] for cut in assignment["cut_nets"]]
        ordinals[0] = (ordinals[0] + 1) % len(self.ir.value["nets"])
        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ), patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            0,
        ), self.assertRaisesRegex(
            ValidationError, "net ordinals are invalid"
        ):
            build_static_exact_semantic_contract(
                self.ir,
                self.platform.to_dict(),
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=assignment["semantic_contract"][
                    "max_cross_fpga_dependency_depth"
                ],
                cut_net_ordinals=ordinals,
            )

    def test_native_semantic_analysis_rejects_forged_capture_coordinate(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        cut_count = len(assignment["cut_nets"])
        payload = bytearray(
            struct.pack(
                "<QIIQ",
                0x315845534F4D5545,
                3,
                cut_count,
                1,
            )
        )
        for _ in range(cut_count):
            payload.extend(struct.pack("<BI", 0, 0))
        # Net 0 sink 0 is combinational logic, not architectural state. The
        # native protocol cannot assert an identity; Python resolves this
        # coordinate against EmuIR and rejects the forged capture.
        payload.extend(struct.pack("<IIII", 0, 0, 0, 0))

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "invalid-static-exact"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"pathlib.Path(sys.argv[2]).write_bytes(bytes.fromhex({payload.hex()!r}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            with patch(
                "emuflow.combinational_cut.resolve_native_stage_command",
                return_value=[str(executable)],
            ), self.assertRaisesRegex(
                ValidationError, "capture is invalid"
            ):
                _analyze_static_exact_dependencies_native(
                    self.ir,
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=assignment["semantic_contract"][
                        "max_cross_fpga_dependency_depth"
                    ],
                    candidate_selection_policy=(
                        STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                    ),
                )

    def test_native_protocol_rejects_huge_header_count_before_allocation(self):
        executable = static_exact_analyzer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            magic = 0x31584553464D5545
            maximum = 2**32 - 1
            malformed = {
                "instance": struct.pack(
                    "<Q6I", magic, 3, maximum, 0, 0, 1, 0
                ),
                "net": struct.pack(
                    "<Q6I", magic, 3, 0, maximum, 0, 1, 0
                ),
                "cut": struct.pack(
                    "<Q6I", magic, 3, 0, 0, maximum, 1, 0
                ),
                "driver": (
                    struct.pack("<Q6I", magic, 3, 0, 1, 0, 1, 0)
                    + struct.pack("<II", maximum, 0)
                ),
                "endpoint": (
                    struct.pack("<Q6I", magic, 3, 0, 1, 0, 1, 1)
                    + struct.pack("<II", 0, maximum)
                ),
                "cut-sink": (
                    struct.pack("<Q6I", magic, 3, 0, 1, 1, 1, 0)
                    + struct.pack("<II", 0, 0)
                    + struct.pack("<III", 0, 0, maximum)
                ),
            }
            for label, payload in malformed.items():
                with self.subTest(label=label):
                    native_input = root / f"huge-{label}.in"
                    native_output = root / f"huge-{label}.out"
                    native_input.write_bytes(payload)
                    completed = subprocess.run(
                        [executable, native_input, native_output],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("count is invalid", completed.stderr)
                    self.assertFalse(native_output.exists())

    def test_native_protocol_rejects_huge_output_capture_count(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        cut_count = len(assignment["cut_nets"])
        payload = bytearray(
            struct.pack(
                "<QIIQ",
                0x315845534F4D5545,
                3,
                cut_count,
                2**64 - 1,
            )
        )
        for _ in range(cut_count):
            payload.extend(struct.pack("<BI", 0, 0))
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "huge-static-exact-output"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                f"pathlib.Path(sys.argv[2]).write_bytes(bytes.fromhex({payload.hex()!r}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            with patch(
                "emuflow.combinational_cut.resolve_native_stage_command",
                return_value=[str(executable)],
            ), self.assertRaisesRegex(
                ValidationError, "capture count is invalid"
            ):
                _analyze_static_exact_dependencies_native(
                    self.ir,
                    assignment["instance_assignment"],
                    assignment["cut_nets"],
                    max_dependency_depth=assignment["semantic_contract"][
                        "max_cross_fpga_dependency_depth"
                    ],
                    candidate_selection_policy=(
                        STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                    ),
                )

    def test_native_protocol_preserves_unicode_identity_and_8192_bit_capture(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        value = copy.deepcopy(self.ir.value)
        instance_names = {
            item["id"]: f"实例/{item['id']}🙂" for item in value["instances"]
        }
        net_names = {item["id"]: f"网络/{item['id']}λ" for item in value["nets"]}
        for instance in value["instances"]:
            instance["id"] = instance_names[instance["id"]]
        for net in value["nets"]:
            old_net = net["id"]
            net["id"] = net_names[old_net]
            net["name"] = net_names[old_net]
            for endpoint in [*net["drivers"], *net["sinks"]]:
                if endpoint["instance"] is not None:
                    endpoint["instance"] = instance_names[endpoint["instance"]]
            if old_net == "d":
                net["sinks"][0]["bit"] = 1 << 8191
        unicode_ir = EmuIR(value)
        unicode_assignment = {
            instance_names[name]: fpga
            for name, fpga in assignment["instance_assignment"].items()
        }
        unicode_cuts = [
            {**cut, "net": net_names[cut["net"]]}
            for cut in assignment["cut_nets"]
        ]
        arguments = (
            unicode_ir,
            self.platform.to_dict(),
            unicode_assignment,
            unicode_cuts,
        )
        keywords = {
            "max_dependency_depth": assignment["semantic_contract"][
                "max_cross_fpga_dependency_depth"
            ]
        }
        with patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            2**63,
        ):
            expected = build_static_exact_semantic_contract(
                *arguments, **keywords
            )
        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ), patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            0,
        ):
            actual = build_static_exact_semantic_contract(*arguments, **keywords)

        self.assertEqual(actual, expected)
        self.assertEqual(
            semantic_contract_sha256(actual),
            semantic_contract_sha256(expected),
        )
        self.assertIn(
            1 << 8191,
            [
                record["bit"]
                for record in actual["capture_requirements"]
                if record["kind"] == "architectural-state"
            ],
        )

    def test_reference_and_native_reject_capture_over_8192_bits(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        value = copy.deepcopy(self.ir.value)
        for net in value["nets"]:
            if net["id"] == "d":
                net["sinks"][0]["bit"] = 1 << 8192
        invalid_ir = EmuIR(value)
        arguments = (
            invalid_ir,
            self.platform.to_dict(),
            assignment["instance_assignment"],
            assignment["cut_nets"],
        )
        keywords = {
            "max_dependency_depth": assignment["semantic_contract"][
                "max_cross_fpga_dependency_depth"
            ]
        }
        with patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            2**63,
        ), self.assertRaisesRegex(ValidationError, "at most 8192 bits"):
            build_static_exact_semantic_contract(*arguments, **keywords)

        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ), patch(
            "emuflow.combinational_cut.STATIC_EXACT_NATIVE_MIN_INSTANCES",
            0,
        ), self.assertRaisesRegex(ValidationError, "at most 8192 bits"):
            build_static_exact_semantic_contract(*arguments, **keywords)

    def test_native_analysis_retains_compact_integer_dependency_relation(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        expected_nodes = assignment["semantic_contract"]["cut_nodes"]
        ordered_cut_ids = [node["net"] for node in expected_nodes]
        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ):
            analysis = _analyze_static_exact_dependencies_native(
                self.ir,
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=assignment["semantic_contract"][
                    "max_cross_fpga_dependency_depth"
                ],
                candidate_selection_policy=(
                    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                ),
            )

        self.assertNotIn("dependencies", analysis)
        self.assertNotIn("local_launches", analysis)
        self.assertNotIn("dependency_level", analysis)
        self.assertNotIn("combinational_depth", analysis)
        records = analysis["analysis_records"]
        self.assertEqual(len(records), len(expected_nodes))
        self.assertEqual(
            records.dependency_edge_count,
            assignment["semantic_contract"]["metrics"]["dependency_edges"],
        )
        for index, expected in enumerate(expected_nodes):
            self.assertEqual(
                [
                    ordered_cut_ids[source]
                    for source in records.dependency_indices(index)
                ],
                expected["predecessor_cut_nets"],
            )
            self.assertEqual(
                records.dependency_level(index), expected["dependency_level"]
            )
            self.assertEqual(
                records.combinational_depth(index),
                expected["combinational_dependency_depth"],
            )

    def test_compact_native_dependency_relation_independently_rejects_cycle(self):
        records = _NativeStaticExactAnalysisRecords(2)
        records.append(False, [1])
        records.append(False, [0])
        with self.assertRaisesRegex(ValidationError, "contains a cycle"):
            records.finalize(bytearray([1, 1]))

    def test_compact_native_analysis_enforces_dependency_depth_limit(self):
        _, assignment = self._exact_artifacts(dependency_depth=2)
        executable = static_exact_analyzer()
        with patch.dict(
            os.environ,
            {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
        ), self.assertRaisesRegex(
            ValidationError, "dependency depth exceeds the configured limit"
        ):
            _analyze_static_exact_dependencies_native(
                self.ir,
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=1,
                candidate_selection_policy=(
                    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                ),
            )

    def test_native_capture_resolution_only_materializes_requested_sinks(self):
        class UnrequestedEndpoint(dict):
            def __getitem__(self, key):
                raise AssertionError("unrequested endpoint was expanded")

        nets = [
            {
                "sinks": [UnrequestedEndpoint() for _ in range(128)]
                + [
                    {
                        "instance": "state0",
                        "port": "D",
                        "bit": 3,
                    }
                ]
            }
        ]
        self.assertEqual(
            _resolve_native_capture_requests(
                nets,
                [(128, 1, 0), (128, 0, 1)],
                ["net0", "net1"],
                ["fpga0", "fpga1"],
            ),
            [
                (
                    "net1",
                    "fpga0",
                    "architectural-state",
                    "state0",
                    "D",
                    3,
                ),
                (
                    "net0",
                    "fpga1",
                    "architectural-state",
                    "state0",
                    "D",
                    3,
                ),
            ],
        )
        with self.assertRaisesRegex(ValidationError, "capture is invalid"):
            _resolve_native_capture_requests(
                nets,
                [(129, 0, 0)],
                ["net0"],
                ["fpga0"],
            )

    def test_native_capture_requests_are_fixed_width_and_canonical(self):
        requests = _NativeStaticExactCaptureRequests()
        requests.append(2, 0, 1)
        requests.append(2, 1, 0)
        requests.append(9, 0, 0)
        self.assertEqual(requests.payload_bytes, 3 * 3 * 4)
        self.assertEqual(
            list(requests),
            [(2, 0, 1), (2, 1, 0), (9, 0, 0)],
        )
        with self.assertRaisesRegex(ValidationError, "capture is invalid"):
            requests.append(9, 0, 0)
        out_of_order = _NativeStaticExactCaptureRequests()
        out_of_order.append(4, 0, 0)
        with self.assertRaisesRegex(ValidationError, "capture is invalid"):
            out_of_order.append(3, 1, 0)
        requests.clear()
        self.assertEqual(len(requests), 0)
        self.assertEqual(requests.payload_bytes, 0)

    def test_native_capture_stream_canonicalizes_without_tuple_population(self):
        nets = [
            {
                "sinks": [
                    {"instance": "state_z", "port": "D", "bit": 3},
                    {"instance": None, "port": "out", "bit": 0},
                ]
            }
        ]
        requests = [(0, 1, 0), (1, 0, 1), (0, 1, 0)]
        materialized_relation = sorted(
            set(
                _resolve_native_capture_requests(
                    nets,
                    requests,
                    ["net_a", "net_z"],
                    ["fpga_a", "fpga_z"],
                )
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            streamed = _resolve_native_capture_stream(
                nets,
                requests,
                ["net_a", "net_z"],
                ["fpga_a", "fpga_z"],
                Path(temporary) / "captures.sqlite3",
            )

        self.assertIsInstance(streamed, StaticExactRecordSequence)
        self.assertEqual(
            list(streamed),
            list(build_static_exact_capture_stream(materialized_relation)),
        )
        self.assertEqual(len(streamed), len(materialized_relation))
        self.assertEqual(requests, [])

    def test_native_capture_stream_rejects_invalid_architectural_bit(self):
        for bit in (True, 1 << 8192):
            nets = [
                {
                    "sinks": [
                        {"instance": "state0", "port": "D", "bit": bit}
                    ]
                }
            ]
            with self.subTest(bit=bit), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(
                    ValidationError, "at most 8192 bits"
                ):
                    _resolve_native_capture_stream(
                        nets,
                        [(0, 0, 0)],
                        ["net0"],
                        ["fpga0"],
                        Path(temporary) / "captures.sqlite3",
                    )

    def test_native_capture_stream_rejects_non_utf8_before_sqlite(self):
        nets = [
            {
                "sinks": [
                    {"instance": "state\ud800", "port": "D", "bit": 0}
                ]
            }
        ]
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            ValidationError, "capture identity is invalid"
        ):
            _resolve_native_capture_stream(
                nets,
                [(0, 0, 0)],
                ["net0"],
                ["fpga0"],
                Path(temporary) / "captures.sqlite3",
            )

    def test_semantic_builder_transfers_owned_analysis_maps(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        owned = {}

        def analyze(*args, **kwargs):
            result = _analyze_static_exact_dependencies(*args, **kwargs)
            for name in (
                "cut_by_net",
                "dependencies",
                "local_launches",
                "dependency_level",
                "combinational_depth",
            ):
                owned[name] = result[name]
            return result

        with patch(
            "emuflow.combinational_cut._analyze_static_exact_dependencies",
            side_effect=analyze,
        ):
            actual = build_static_exact_semantic_contract(
                self.ir,
                self.platform.to_dict(),
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=assignment["semantic_contract"][
                    "max_cross_fpga_dependency_depth"
                ],
            )

        self.assertEqual(actual, assignment["semantic_contract"])
        for name, mapping in owned.items():
            self.assertEqual(mapping, {}, name)

    def test_semantic_builder_can_stream_derived_records(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        expected = assignment["semantic_contract"]

        actual = build_static_exact_semantic_contract(
            self.ir,
            self.platform.to_dict(),
            assignment["instance_assignment"],
            assignment["cut_nets"],
            max_dependency_depth=expected[
                "max_cross_fpga_dependency_depth"
            ],
            stream_records=True,
        )

        self.assertIsInstance(
            actual["dependency_edges"], StaticExactRecordSequence
        )
        self.assertIsInstance(actual["cut_nodes"], StaticExactCutNodeSequence)
        self.assertIsInstance(
            actual["logic_segments"], StaticExactRecordSequence
        )
        self.assertIsInstance(
            actual["capture_requirements"], StaticExactRecordSequence
        )
        self.assertEqual(
            list(actual["dependency_edges"]), expected["dependency_edges"]
        )
        self.assertEqual(
            list(actual["logic_segments"]), expected["logic_segments"]
        )
        self.assertEqual(
            list(actual["capture_requirements"]),
            expected["capture_requirements"],
        )
        self.assertEqual(
            semantic_contract_sha256(actual),
            assignment["semantic_contract_sha256"],
        )

    def test_indexed_assignment_storage_reconstructs_exact_contract(self):
        clusters, assignment = self._exact_artifacts(dependent_return=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index_summary = write_phase3_assignment_index(
                root / "assignment.sqlite", self.ir, assignment
            )
            packed = pack_phase3_assignment(
                assignment,
                clusters,
                cut_nets_index={
                    "artifact": "assignment.sqlite",
                    "schema": "emuflow.phase3-cut-records/v1",
                    "records": index_summary["cut_records"],
                    "sha256": index_summary["cut_records_sha256"],
                },
            )
            self.assertEqual(packed["schema"], PACKED_ASSIGNMENT_SCHEMA)
            self.assertNotIn("cut_nets", packed["document"])
            self.assertIsNone(packed["semantic_contract"])
            self.assertEqual(
                packed["semantic_contract_index"],
                {
                    "artifact": "assignment.sqlite",
                    "schema": STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
                    "sha256": assignment["semantic_contract_sha256"],
                },
            )
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            self.assertEqual(read_json(root / "assignment.json"), assignment)

            route_view = read_phase3_assignment_view(
                root / "assignment.json",
                include_semantic_contract=False,
            )
            self.assertTrue(route_view["cut_records_indexed"])
            self.assertIsInstance(route_view["cut_nets"], list)
            self.assertEqual(list(route_view["cut_nets"]), assignment["cut_nets"])
            self.assertEqual(list(route_view["cut_nets"]), assignment["cut_nets"])
            self.assertTrue(route_view["semantic_contract_indexed"])
            self.assertNotIn("semantic_contract", route_view)
            self.assertEqual(
                route_view["semantic_contract_schema"],
                STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
            )

            cut_tampered = copy.deepcopy(packed)
            cut_tampered["cut_nets_index"]["sha256"] = "0" * 64
            write_json(root / "assignment.json", cut_tampered, compact=True)
            with self.assertRaisesRegex(ValueError, "identity disagrees"):
                read_phase3_assignment_view(root / "assignment.json")

            packed["semantic_contract_index"]["sha256"] = "0" * 64
            write_json(root / "assignment.json", packed, compact=True)
            with self.assertRaisesRegex(ValueError, "index is malformed"):
                read_json(root / "assignment.json")

            with closing(sqlite3.connect(root / "assignment.sqlite")) as connection, connection:
                connection.execute(
                    "UPDATE cuts SET sink_endpoints=sink_endpoints+1 "
                    "WHERE cut_index=0"
                )
            with self.assertRaisesRegex(
                ValidationError, "cut-record identity disagrees"
            ):
                validate_phase3_assignment_index(root / "assignment.sqlite")

    def test_indexed_assignment_storage_preserves_zero_cut_contract(self):
        clusters, assignment = self._exact_artifacts(dependent_return=True)
        assignment = copy.deepcopy(assignment)
        assignment["cut_nets"] = []
        assignment.pop("semantic_contract", None)
        assignment.pop("semantic_contract_sha256", None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary = write_phase3_assignment_index(
                root / "assignment.sqlite", self.ir, assignment
            )
            self.assertEqual(summary["cut_records"], 0)
            packed = pack_phase3_assignment(
                assignment,
                clusters,
                cut_nets_index={
                    "artifact": "assignment.sqlite",
                    "schema": "emuflow.phase3-cut-records/v1",
                    "records": 0,
                    "sha256": summary["cut_records_sha256"],
                },
            )
            write_json(
                root / "clusters.json",
                pack_phase3_clusters(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            view = read_phase3_assignment_view(root / "assignment.json")
            self.assertEqual(len(view["cut_nets"]), 0)
            self.assertEqual(list(view["cut_nets"]), [])
            self.assertEqual(read_json(root / "assignment.json"), assignment)

    def _legacy_exact_assignment_with_bit(self, bit):
        _, assignment = self._exact_artifacts(dependent_return=True)
        assignment = copy.deepcopy(assignment)
        capture = next(
            item
            for item in assignment["semantic_contract"][
                "capture_requirements"
            ]
            if item["kind"] == "architectural-state"
        )
        capture["bit"] = bit
        assignment["semantic_contract_sha256"] = semantic_contract_sha256(
            assignment["semantic_contract"]
        )
        return assignment, capture["id"], capture["fpga"]

    def test_legacy_migration_uses_canonical_v2_bit_storage_boundaries(self):
        cases = (
            ((1 << 63) - 1, "integer", None),
            (1 << 63, "blob", 8),
            (1 << 8191, "blob", 1024),
        )
        for bit, storage_type, storage_bytes in cases:
            with self.subTest(bit_length=bit.bit_length()):
                assignment, capture_id, fpga = (
                    self._legacy_exact_assignment_with_bit(bit)
                )
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    ir_path = root / "design.emuir.sqlite"
                    assignment_path = root / "assignment.json"
                    index_path = root / "assignment.sqlite"
                    write_compact_emuir(ir_path, self.ir.to_dict())
                    write_json(assignment_path, assignment)

                    summary = migrate_phase3_assignment_index(
                        index_path, ir_path, assignment_path
                    )
                    self.assertEqual(summary["status"], "pass")
                    with closing(sqlite3.connect(index_path)) as connection, connection:
                        stored = connection.execute(
                            "SELECT typeof(bit),CASE WHEN typeof(bit)='blob' "
                            "THEN length(bit) END "
                            "FROM exact_capture_requirements "
                            "WHERE capture_id=?",
                            (capture_id,),
                        ).fetchone()
                    self.assertEqual(stored, (storage_type, storage_bytes))

                    partition = read_phase3_exact_semantic_partition(
                        index_path, fpga
                    )
                    observed = next(
                        item
                        for item in partition["capture_requirements"]
                        if item["id"] == capture_id
                    )
                    self.assertEqual(observed["bit"], bit)
                    reconstructed = read_phase3_exact_semantic_contract(
                        index_path, assignment
                    )
                    self.assertEqual(
                        reconstructed["capture_requirements"],
                        assignment["semantic_contract"][
                            "capture_requirements"
                        ],
                    )
                    self.assertEqual(
                        validate_phase3_assignment_index(index_path)["status"],
                        "pass",
                    )

    def test_legacy_json_migration_rejects_8193_bit_capture_before_digest(self):
        assignment, _, _ = self._legacy_exact_assignment_with_bit(0)
        capture = next(
            item
            for item in assignment["semantic_contract"][
                "capture_requirements"
            ]
            if item["kind"] == "architectural-state"
        )
        capture["bit"] = 1 << 8192
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            index_path = root / "assignment.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            write_json(assignment_path, assignment)
            with patch(
                "emuflow.phase3_assignment_index.semantic_contract_sha256",
                side_effect=AssertionError("digest must follow capture validation"),
            ), self.assertRaisesRegex(ValidationError, "at most 8192 bits"):
                migrate_phase3_assignment_index(
                    index_path, ir_path, assignment_path
                )
            self.assertFalse(index_path.exists())

    def test_legacy_v1_and_v2_reads_reject_oversized_capture_bits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            v1_path = root / "assignment-v1.sqlite"
            for bit, accepted in ((1 << 8191, True), (1 << 8192, False)):
                if v1_path.exists():
                    v1_path.unlink()
                capture = {
                    "id": "capture000000",
                    "cut_net": "net0",
                    "fpga": "fpga0",
                    "kind": "architectural-state",
                    "endpoint": "state/D",
                    "port": "D",
                    "bit": bit,
                }
                with closing(sqlite3.connect(v1_path)) as connection, connection:
                    connection.executescript(
                        f"""
                        PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};
                        PRAGMA user_version=1;
                        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                        CREATE TABLE parts(
                          part_index INTEGER PRIMARY KEY,fpga TEXT
                        );
                        CREATE TABLE copies(
                          source_instance_seq INTEGER,part_index INTEGER,
                          copy_id TEXT,is_replica INTEGER
                        );
                        CREATE TABLE cut_sinks(
                          net_id TEXT,source_part_index INTEGER,
                          sink_part_index INTEGER
                        );
                        CREATE TABLE exact_segment_bindings(
                          segment_id TEXT,part_index INTEGER,payload_json TEXT
                        );
                        CREATE TABLE exact_capture_requirements(
                          capture_id TEXT,part_index INTEGER,payload_json TEXT
                        );
                        """
                    )
                    connection.executemany(
                        "INSERT INTO metadata VALUES (?,?)",
                        [
                            (
                                "schema",
                                json.dumps(
                                    "emuflow.phase3-assignment-sqlite/v1"
                                ),
                            ),
                            ("design", json.dumps("legacy")),
                            ("platform", json.dumps("test")),
                            ("source_instance_count", json.dumps(0)),
                            ("semantic_contract_schema", json.dumps("schema")),
                            ("semantic_contract_sha256", json.dumps("0" * 64)),
                        ],
                    )
                    connection.execute(
                        "INSERT INTO parts VALUES (0,'fpga0')"
                    )
                    connection.execute(
                        "INSERT INTO exact_capture_requirements VALUES (?,?,?)",
                        ("capture000000", 0, json.dumps(capture)),
                    )
                if accepted:
                    partition = read_phase3_exact_semantic_partition(
                        v1_path, "fpga0"
                    )
                    self.assertEqual(
                        partition["capture_requirements"][0]["bit"], bit
                    )
                    self.assertEqual(
                        validate_phase3_assignment_index(v1_path)["status"],
                        "pass",
                    )
                else:
                    for reader in (
                        lambda: read_phase3_exact_semantic_partition(
                            v1_path, "fpga0"
                        ),
                        lambda: validate_phase3_assignment_index(v1_path),
                    ):
                        with self.assertRaisesRegex(
                            ValidationError, "at most 8192 bits"
                        ):
                            reader()

            assignment, capture_id, fpga = (
                self._legacy_exact_assignment_with_bit(0)
            )
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            v2_path = root / "assignment-v2.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            write_json(assignment_path, assignment)
            migrate_phase3_assignment_index(
                v2_path, ir_path, assignment_path
            )
            invalid_blobs = (
                (b"\x00", "encoding is invalid"),
                (b"\x01", "encoding is invalid"),
                (((1 << 63) - 1).to_bytes(8, "big"), "encoding is invalid"),
                (b"\x01" + b"\x00" * 1024, "at most 8192 bits"),
            )
            for encoded, message in invalid_blobs:
                with self.subTest(encoded_bytes=len(encoded), message=message):
                    with closing(sqlite3.connect(v2_path)) as connection, connection:
                        connection.execute(
                            "UPDATE exact_capture_requirements SET bit=? "
                            "WHERE capture_id=?",
                            (sqlite3.Binary(encoded), capture_id),
                        )
                    readers = (
                        lambda: read_phase3_exact_semantic_partition(
                            v2_path, fpga
                        ),
                        lambda: read_phase3_exact_semantic_contract(
                            v2_path, assignment
                        ),
                        lambda: validate_phase3_assignment_index(v2_path),
                    )
                    for reader in readers:
                        with self.assertRaisesRegex(ValidationError, message):
                            reader()

    def test_indexed_assignment_publish_reuses_owned_exact_digest(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        with tempfile.TemporaryDirectory() as temporary_directory, patch(
            "emuflow.phase3_assignment_index.semantic_contract_sha256",
            side_effect=AssertionError(
                "production publication must not rehash the resident contract"
            ),
        ):
            write_phase3_assignment_index(
                Path(temporary_directory) / "assignment.sqlite",
                self.ir,
                assignment,
            )

    def _exact_artifacts(
        self, frame_slots=16, dependent_return=False, dependency_depth=1
    ):
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=dependency_depth,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga1",
            "l2": "fpga0" if dependency_depth == 2 else "fpga1",
            "q1": (
                "fpga0"
                if dependent_return or dependency_depth == 2
                else "fpga1"
            ),
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            if (
                cluster_id in cluster_targets
                and cluster_targets[cluster_id] != target
            ):
                raise AssertionError("fixture assigns one cluster twice")
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-static-exact-v1",
            seed=0,
        )
        return clusters, assignment

    def test_removed_legacy_candidate_policy_is_rejected(self):
        with self.assertRaisesRegex(
            ValidationError, "unknown static exact candidate policy"
        ):
            build_clusters(
                self.ir,
                self.constraints,
                cut_mode=CUT_MODE_STATIC_EXACT,
                static_exact_candidate_policy="potential-frontier-depth-v1",
            )

    def _exact_routes(self, assignment, frame_slots=16):
        demands = demands_from_assignment(assignment, self.platform)
        constraints = normalize_route_constraints(
            None, self.platform, frame_slots=frame_slots
        )
        utilization = {
            "link_0_1:fpga0->fpga1": 0,
            "link_0_1:fpga1->fpga0": 0,
        }
        routed = []
        for demand in demands:
            source = demand["source"]
            sink = demand["sinks"][0]
            key = f"link_0_1:{source}->{sink}"
            utilization[key] += 1
            routed.append(
                {
                    **demand,
                    "tree_edges": [
                        {
                            "link": "link_0_1",
                            "from": source,
                            "to": sink,
                        }
                    ],
                    "max_latency_cycles": 2,
                }
            )
        _, _, capacities = build_directed_graph(
            self.platform,
            constraints,
            reserve_arrival_and_commit=True,
        )
        link_utilization = []
        for key in sorted(utilization):
            used = utilization[key]
            link_utilization.append(
                {
                    **capacities[key],
                    "used_bits": used,
                    "utilization": used / capacities[key]["capacity_bits"],
                }
            )
        routes = {
            "schema": "emuflow.system-routes/v1",
            "design": assignment["design"],
            "platform": self.platform.name,
            "provider": "native-load-balanced-multicast-v2",
            "constraints": constraints,
            "demands": demands,
            "routes": routed,
            "link_utilization": link_utilization,
            "metrics": {
                "demands": len(demands),
                "routed_sinks": len(demands),
                "tree_edges": len(demands),
                "max_link_utilization": max(
                    item["utilization"] for item in link_utilization
                ),
                "total_link_bit_hops": len(demands),
                "iterations": 1,
            },
            "semantic_contract_schema": assignment["semantic_contract"][
                "schema"
            ],
            "semantic_contract_sha256": assignment[
                "semantic_contract_sha256"
            ],
        }
        validate_system_routes(assignment, self.platform, routes)
        # Low-level TDM tests exercise the solver directly with an in-memory
        # view. Persisted Phase 4 artifacts retain only the schema/digest
        # binding and are hydrated by run_phase5 from assignment.json.
        return {**routes, "semantic_contract": assignment["semantic_contract"]}

    def test_exact_routing_uses_dependency_dag_not_global_round_windows(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        constraints = normalize_route_constraints(
            None, self.platform, frame_slots=16
        )
        routes = route_system_native(
            assignment,
            self.platform,
            constraints,
            executable=str(tlr_router()),
        )
        self.assertNotIn("round_capacity_contract", routes)
        self.assertEqual(
            {
                item["capacity_bits"]
                for item in routes["link_utilization"]
            },
            {32 * 8},
        )

        checked = validate_system_routes(assignment, self.platform, routes)
        self.assertEqual(checked["status"], "pass")
        hydrated = {
            **routes,
            "semantic_contract": assignment["semantic_contract"],
        }
        hydrated["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 100.0,
                "negative_slack_scale_ns": 100.0,
                "max_clock_period_ns": 100.0,
            },
            "compression": {"original_paths": 1, "compressed_paths": 1},
            "paths": [
                {
                    "path": "cross-layer-path",
                    "clock_domain": "clk",
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": 1.0,
                    "cut_nets": ["n0", "d"],
                    "cut_transitions": [
                        {"net": "n0", "from": "fpga0", "to": "fpga1"},
                        {"net": "d", "from": "fpga1", "to": "fpga0"},
                    ],
                }
            ],
        }
        plan = build_tdm_ratio_plan(
            hydrated,
            self.platform,
            executable=str(tdm_ratio_optimizer()),
        )
        self.assertEqual(
            plan["round_barrier_legalization"]["source_ready_semantics"],
            "dependency-dag-only",
        )
        self.assertIsNone(
            plan["round_barrier_legalization"]["source_ready_slot"]
        )

    def test_phase4_indexed_contract_view_matches_full_demands(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        expected = demands_from_assignment(assignment, self.platform)
        indexed = copy.deepcopy(assignment)
        schema = indexed.pop("semantic_contract")["schema"]
        indexed["semantic_contract_indexed"] = True
        indexed["semantic_contract_schema"] = schema
        self.assertEqual(
            demands_from_assignment(indexed, self.platform), expected
        )
        routes = self._exact_routes(assignment)
        routes.pop("semantic_contract")
        self.assertEqual(
            validate_system_routes(indexed, self.platform, routes)["status"],
            "pass",
        )

    def test_phase5_indexed_contract_accepts_register_output_launches(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schema = routes["semantic_contract"]["schema"]
        routes["semantic_contract"] = {
            "schema": schema,
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        routes["routes"][0]["cut_class"] = "register_output"
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            schedule["provider"], TDM_EXACT_DEPENDENCY_PROVIDER
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_native_indexed_exact_schedule_matches_python_reference(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schema = routes["semantic_contract"]["schema"]
        routes["semantic_contract"] = {
            "schema": schema,
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        executable = tdm_exact_scheduler()

        def compare(candidate):
            reference = build_tdm_schedule(candidate, self.platform)
            with (
                patch.dict(
                    os.environ,
                    {"EMUFLOW_NATIVE_ROOT": str(executable.parent.parent)},
                ),
                patch("emuflow.tdm.NATIVE_EXACT_SCHEDULE_MIN_ROUTES", 0),
            ):
                native = build_tdm_schedule(candidate, self.platform)
            self.assertIsInstance(native["entries"], list)
            self.assertIsNot(type(native["entries"]), list)
            self.assertGreater(len(native["entries"]), 0)
            self.assertEqual(list.__len__(native["entries"]), 0)
            self.assertIsInstance(native["demand_completions"], list)
            self.assertIsNot(type(native["demand_completions"]), list)
            self.assertEqual(
                list.__len__(native["demand_completions"]), 0
            )
            self.assertEqual(native, reference)
            self.assertEqual(json.loads(json.dumps(native)), reference)
            self.assertEqual(
                validate_tdm_schedule(
                    candidate, self.platform, native
                )["status"],
                "pass",
            )
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "schedule.json"
                write_tdm_schedule_storage(path, native)
                self.assertEqual(
                    read_tdm_schedule(path, materialize=True), reference
                )
                indexed_schedule = read_tdm_schedule(path)
                if isinstance(candidate, SystemRoutesView):
                    with (
                        patch(
                            "emuflow.tdm._indexed_exact_nodes",
                            side_effect=AssertionError(
                                "typed validation rebuilt an R-sized node map"
                            ),
                        ),
                        patch(
                            "emuflow.tdm._exact_dependency_order",
                            side_effect=AssertionError(
                                "typed validation rebuilt an R-sized order"
                            ),
                        ),
                    ):
                        self.assertEqual(
                            validate_tdm_schedule(
                                candidate,
                                self.platform,
                                indexed_schedule,
                            )["status"],
                            "pass",
                        )
                else:
                    self.assertEqual(
                        validate_tdm_schedule(
                            candidate,
                            self.platform,
                            indexed_schedule,
                        )["status"],
                        "pass",
                    )

        compare(routes)
        template = next(
            route
            for route in routes["routes"]
            if not route["predecessor_cut_nets"]
        )
        repeated = copy.deepcopy(routes)
        repeated["routes"] = []
        for index in range(300):
            route = copy.deepcopy(template)
            route["id"] = f"repeated-demand-{index:04d}"
            route["net"] = f"repeated-net-{index:04d}"
            route["predecessor_cut_nets"] = []
            route["dependency_level"] = 0
            repeated["routes"].append(route)
        repeated["demands"] = [
            {
                key: copy.deepcopy(route[key])
                for key in (
                    "id",
                    "net",
                    "source",
                    "sinks",
                    "width_bits",
                    "cut_class",
                    "dependency_level",
                    "combinational_dependency_depth",
                    "predecessor_cut_nets",
                )
            }
            for route in repeated["routes"]
        ]
        compare(repeated)
        with tempfile.TemporaryDirectory() as temporary:
            route_path = Path(temporary) / "routes.json"
            write_system_route_storage(route_path, repeated)
            indexed_routes = read_system_routes(route_path)
            self.assertIsInstance(indexed_routes, SystemRoutesView)
            self.assertEqual(list.__len__(indexed_routes["routes"]), 0)
            compare(indexed_routes)

    @staticmethod
    def _persisted_routes(routes):
        persisted = copy.deepcopy(routes)
        persisted.pop("semantic_contract", None)
        return persisted

    def test_safe_default_is_identical_to_explicit_safe_mode(self):
        implicit = build_clusters(self.ir, self.constraints)
        explicit = build_clusters(
            self.ir, self.constraints, cut_mode="sequential-only"
        )
        self.assertEqual(implicit, explicit)
        self.assertNotIn("cut_mode", implicit["policy"])

    def test_exact_cluster_hot_path_does_not_hash_the_complete_emuir(self):
        clusters = build_clusters(
            self.ir,
            self.constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
        )
        self.assertNotIn(
            "characterization_source_sha256", clusters["policy"]
        )

    def test_exact_mode_accepts_multiple_virtual_clocks(self):
        value = copy.deepcopy(self.ir.value)
        value["clocks"] = [
            *self.ir.value["clocks"],
            {
                "id": "aux_clk",
                "name": "aux_clk",
                "source_port": "aux_clk",
                "period_ns": None,
            },
        ]
        clusters = build_clusters(
            EmuIR(value),
            self.constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
        )
        self.assertEqual(
            clusters["policy"]["cut_mode"], CUT_MODE_STATIC_EXACT
        )

    def test_depth_one_cut_has_independently_validated_contract(self):
        safe = build_clusters(self.ir, self.constraints)
        clusters, assignment = self._exact_artifacts()
        self.assertGreater(
            len(clusters["clusters"]), len(safe["clusters"])
        )
        combinational = [
            item
            for item in assignment["cut_nets"]
            if item["cut_class"] == "combinational"
        ]
        self.assertEqual([item["net"] for item in combinational], ["n0"])
        self.assertEqual(combinational[0]["predecessor_cut_nets"], [])
        self.assertEqual(combinational[0]["combinational_dependency_depth"], 1)
        self.assertTrue(
            all(
                "transport_round" not in item
                for item in assignment["cut_nets"]
            )
        )
        self.assertNotIn("transport_rounds", assignment["metrics"])
        self.assertNotIn("round_barriers", assignment["metrics"])
        self.assertEqual(
            assignment["metrics"]["transport_ordering_policy"],
            "path-local-dependency-dag-v1",
        )
        contract = assignment["semantic_contract"]
        self.assertEqual(
            contract["qualification"],
            "structural-partition-legality",
        )
        self.assertEqual(contract["metrics"]["combinational_cut_nets"], 1)
        self.assertTrue(contract["capture_requirements"])
        validation = validate_partition_artifacts(
            self.ir, self.platform, clusters, assignment
        )
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(
            validation["qualification"],
            "structural-partition-legality",
        )

    def test_exact_contract_preserves_reached_memory_capture_pin(self):
        value = copy.deepcopy(self.ir.value)
        memory = next(item for item in value["instances"] if item["id"] == "q1")
        memory.update(
            {
                "type": "VTR_DP_RAM",
                "resources": {"bram": 1},
                "parameters": {"ADDR_WIDTH": 4, "DATA_WIDTH": 8},
            }
        )
        terminal = next(item for item in value["nets"] if item["id"] == "d")
        terminal["sinks"] = [
            {"instance": "q1", "port": "data1", "bit": 3}
        ]
        self.ir = EmuIR(value)
        self.constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        _clusters, assignment = self._exact_artifacts()
        captures = [
            item
            for item in assignment["semantic_contract"]["capture_requirements"]
            if item["kind"] == "architectural-state"
            and item["endpoint"] == "q1"
        ]
        self.assertEqual(len(captures), 1)
        self.assertEqual(
            captures,
            [
                {
                    "id": "capture000000",
                    "cut_net": "n0",
                    "fpga": "fpga1",
                    "kind": "architectural-state",
                    "endpoint": "q1",
                    "port": "data1",
                    "bit": 3,
                }
            ],
        )

    def test_downstream_cut_keeps_local_source_fpga_capture(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "local_cut_fanout",
                    "top": "local_cut_fanout",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {"id": "q0", "type": "FDRE", "resources": {"ff": 1}},
                    {"id": "l0", "type": "LUT2", "resources": {"lut": 1}},
                    {"id": "l1", "type": "LUT2", "resources": {"lut": 1}},
                    {
                        "id": "local_q",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                    },
                    {
                        "id": "remote_q",
                        "type": "FDRE",
                        "resources": {"ff": 1},
                    },
                ],
                "nets": [
                    {
                        "id": "launch",
                        "name": "launch",
                        "cut_class": "register_output",
                        "drivers": [_endpoint("q0", "Q")],
                        "sinks": [_endpoint("l0", "I0")],
                    },
                    {
                        "id": "cut0",
                        "name": "cut0",
                        "cut_class": "combinational",
                        "drivers": [_endpoint("l0", "O")],
                        "sinks": [_endpoint("l1", "I0")],
                    },
                    {
                        "id": "cut1",
                        "name": "cut1",
                        "cut_class": "combinational",
                        "drivers": [_endpoint("l1", "O")],
                        "sinks": [
                            _endpoint("local_q", "D"),
                            _endpoint("remote_q", "D"),
                        ],
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
        contract = build_static_exact_semantic_contract(
            ir,
            _three_fpga_platform("star").to_dict(),
            {
                "q0": "fpga0",
                "l0": "fpga0",
                "l1": "fpga1",
                "local_q": "fpga1",
                "remote_q": "fpga2",
            },
            [
                {
                    "net": "cut0",
                    "source_fpgas": ["fpga0"],
                    "sink_fpgas": ["fpga1"],
                },
                {
                    "net": "cut1",
                    "source_fpgas": ["fpga1"],
                    "sink_fpgas": ["fpga2"],
                },
            ],
            max_dependency_depth=2,
            candidate_selection_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        captures = {
            (item["cut_net"], item["fpga"], item["endpoint"])
            for item in contract["capture_requirements"]
        }
        self.assertIn(("cut0", "fpga1", "local_q"), captures)
        self.assertIn(("cut1", "fpga2", "remote_q"), captures)

    def test_contract_tamper_is_rejected(self):
        clusters, assignment = self._exact_artifacts()
        tampered = copy.deepcopy(assignment)
        tampered["semantic_contract"]["cut_nodes"][0][
            "dependency_level"
        ] += 1
        with self.assertRaisesRegex(ValidationError, "semantic_contract"):
            validate_partition_artifacts(
                self.ir, self.platform, clusters, tampered
            )

    def test_exact_cluster_policy_tamper_is_rejected(self):
        clusters, assignment = self._exact_artifacts()
        tampered = copy.deepcopy(clusters)
        tampered["policy"]["eligible_combinational_cut_nets"].append("n1")
        with self.assertRaisesRegex(ValidationError, "reconstruction"):
            validate_partition_artifacts(
                self.ir, self.platform, tampered, assignment
            )

    def test_depth_two_contract_schedule_and_macro_cycle_are_exact(self):
        clusters, assignment = self._exact_artifacts(dependency_depth=2)
        combinational = [
            item
            for item in assignment["cut_nets"]
            if item["cut_class"] == "combinational"
        ]
        self.assertEqual([item["net"] for item in combinational], ["n0", "n1"])
        self.assertEqual(combinational[1]["predecessor_cut_nets"], ["n0"])
        self.assertEqual(
            assignment["semantic_contract"]["metrics"][
                "maximum_combinational_dependency_depth"
            ],
            2,
        )
        self.assertEqual(
            validate_partition_artifacts(
                self.ir, self.platform, clusters, assignment
            )["status"],
            "pass",
        )
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )
        readiness = {
            item["net"]: item
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        self.assertEqual(readiness["n1"]["evidence"][0]["predecessor_cut_net"], "n0")
        self.assertGreater(
            readiness["n1"]["source_ready_slot"],
            readiness["n0"]["source_ready_slot"],
        )
        split = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        self.assertEqual(
            validate_split_artifacts(
                self.ir,
                assignment,
                schedule,
                self.platform,
                split,
            )["status"],
            "pass",
        )
        random_evidence = simulate_static_exact_partition_equivalence(
            self.ir, assignment, schedule, cycles=8, seed=23
        )
        self.assertEqual(random_evidence["status"], "pass")
        exhaustive = exhaustively_verify_static_exact_partition_equivalence(
            self.ir, assignment, schedule
        )
        self.assertEqual(exhaustive["status"], "pass")

    def test_v2_releases_deep_potential_candidate_with_actual_depth_one(self):
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        generalized = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        self.assertEqual(
            generalized["policy"]["eligible_combinational_cut_nets"],
            ["n0", "n1"],
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in generalized["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga0",
            "l2": "fpga1",
            "q1": "fpga1",
        }
        cluster_targets = {}
        for instance, target in targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            generalized,
            constraints,
            cluster_targets,
            provider="test-static-exact-v2",
            seed=0,
        )
        contract = assignment["semantic_contract"]
        self.assertEqual(
            contract["schema"],
            STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
        )
        self.assertEqual(contract["metrics"]["combinational_cut_nets"], 1)
        self.assertEqual(
            contract["metrics"]["maximum_combinational_dependency_depth"],
            1,
        )
        cut = next(
            item for item in assignment["cut_nets"] if item["net"] == "n1"
        )
        self.assertEqual(cut["predecessor_cut_nets"], [])
        self.assertEqual(cut["combinational_dependency_depth"], 1)
        self.assertEqual(
            validate_partition_artifacts(
                self.ir, self.platform, generalized, assignment
            )["status"],
            "pass",
        )
        tampered = copy.deepcopy(assignment)
        del tampered["semantic_contract"]["candidate_selection_policy"]
        with self.assertRaisesRegex(ValidationError, "certificate is incomplete"):
            demands_from_assignment(tampered, self.platform)
        routes = self._exact_routes(assignment)
        tampered_routes = copy.deepcopy(routes)
        del tampered_routes["semantic_contract"][
            "candidate_selection_policy"
        ]
        tampered_routes["semantic_contract_sha256"] = semantic_contract_sha256(
            tampered_routes["semantic_contract"]
        )
        with self.assertRaisesRegex(ValidationError, "certificate is incomplete"):
            build_tdm_schedule(tampered_routes, self.platform)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_v2_depth_three_runs_through_schedule_split_and_equivalence(self):
        value = copy.deepcopy(self.ir.value)
        value["instances"].insert(
            -1,
            {
                "id": "l3",
                "type": "LUT2",
                "parameters": {"INIT": "1010"},
                "resources": {"lut": 1},
            },
        )
        terminal = next(item for item in value["nets"] if item["id"] == "d")
        terminal["drivers"] = [_endpoint("l3", "O")]
        value["nets"].insert(
            -1,
            {
                "id": "n2",
                "name": "n2",
                "cut_class": "combinational",
                "drivers": [_endpoint("l2", "O")],
                "sinks": [_endpoint("l3", "I0")],
            },
        )
        self.ir = EmuIR(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=0,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga1",
            "l2": "fpga0",
            "l3": "fpga1",
            "q1": "fpga1",
        }
        cluster_targets = {}
        for instance, target in targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-static-exact-v2",
            seed=0,
        )
        contract = assignment["semantic_contract"]
        self.assertEqual(contract["max_cross_fpga_dependency_depth"], 0)
        self.assertEqual(
            contract["metrics"]["maximum_combinational_dependency_depth"],
            3,
        )
        self.assertEqual(
            [
                item["net"]
                for item in assignment["cut_nets"]
                if item["cut_class"] == "combinational"
            ],
            ["n0", "n1", "n2"],
        )
        self.assertNotIn("uncongested_schedule_lower_bound", contract)
        # Phase 3 records structural legality only.  A frame that is too short
        # is rejected later by the concrete Phase 4/5 route and TDM schedule,
        # not speculatively by partitioning.
        short_frame_contract = build_static_exact_semantic_contract(
            self.ir,
            self.platform.to_dict(),
            assignment["instance_assignment"],
            assignment["cut_nets"],
            max_dependency_depth=0,
            candidate_selection_policy=(STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2),
        )
        self.assertNotIn(
            "uncongested_schedule_lower_bound", short_frame_contract
        )
        with self.assertRaisesRegex(
            ValidationError, "dependency depth exceeds the configured limit"
        ):
            build_static_exact_semantic_contract(
                self.ir,
                self.platform.to_dict(),
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=2,
                candidate_selection_policy=(
                    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                ),
            )
        routes = self._exact_routes(assignment, frame_slots=24)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )
        self.assertEqual(
            schedule["schedule_dependency_certificate"][
                "topological_cut_order"
            ],
            ["n0", "n1", "n2"],
        )
        split = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        self.assertEqual(
            validate_split_artifacts(
                self.ir,
                assignment,
                schedule,
                self.platform,
                split,
            )["status"],
            "pass",
        )
        self.assertEqual(
            exhaustively_verify_static_exact_partition_equivalence(
                self.ir, assignment, schedule
            )["status"],
            "pass",
        )

    def test_reconvergent_depth_two_waits_for_every_predecessor(self):
        ir = _reconvergent_ir()
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            ir,
            self.platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=2,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "qa": "fpga0",
            "qb": "fpga0",
            "qc": "fpga1",
            "la": "fpga0",
            "lb": "fpga0",
            "merge": "fpga1",
            "after": "fpga0",
            "qout": "fpga0",
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            self.assertNotIn(cluster_id, cluster_targets)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            ir,
            self.platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-reconvergent-static-exact-v1",
            seed=0,
        )
        node_by_net = {
            item["net"]: item
            for item in assignment["semantic_contract"]["cut_nodes"]
        }
        self.assertEqual(node_by_net["c"]["predecessor_cut_nets"], ["a", "b"])
        segment_by_id = {
            item["id"]: item
            for item in assignment["semantic_contract"]["logic_segments"]
        }
        source_segments = [
            segment_by_id[item]
            for item in node_by_net["c"]["source_segment_ids"]
        ]
        self.assertEqual(
            [item["kind"] for item in source_segments],
            ["launch_to_tx", "rx_to_tx", "rx_to_tx"],
        )
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        readiness = {
            item["net"]: item
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        evidence = readiness["c"]["evidence"]
        self.assertEqual(
            [
                item["predecessor_cut_net"]
                for item in evidence
                if item["kind"] == "rx_to_tx"
            ],
            ["a", "b"],
        )
        self.assertEqual(
            [item["kind"] for item in evidence],
            ["launch_to_tx", "rx_to_tx", "rx_to_tx"],
        )
        self.assertEqual(
            readiness["c"]["source_ready_slot"],
            max(item["ready_slot"] for item in evidence),
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_one_logical_cut_can_use_two_route_hops_without_depth_inflation(self):
        platform = _three_fpga_platform("line")
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            platform,
        )
        clusters = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            cluster_for["q0"]: "fpga0",
            cluster_for["l0"]: "fpga0",
            cluster_for["l1"]: "fpga2",
            cluster_for["l2"]: "fpga2",
            cluster_for["q1"]: "fpga2",
        }
        assignment = build_partition_assignment(
            self.ir,
            platform,
            clusters,
            constraints,
            targets,
            provider="test-multihop-static-exact-v1",
            seed=0,
        )
        route_constraints = normalize_route_constraints(
            None, platform, frame_slots=16
        )
        routes = route_system_native(
            assignment, platform, route_constraints
        )
        routes = {
            **routes,
            "semantic_contract": assignment["semantic_contract"],
        }
        self.assertEqual(len(routes["routes"]), 1)
        self.assertEqual(len(routes["routes"][0]["tree_edges"]), 2)
        self.assertEqual(
            assignment["semantic_contract"]["metrics"][
                "maximum_combinational_dependency_depth"
            ],
            1,
        )
        schedule = build_tdm_schedule(routes, platform)
        entries = sorted(
            schedule["entries"], key=lambda item: item["hop"]
        )
        self.assertEqual([item["hop"] for item in entries], [0, 1])
        self.assertGreaterEqual(
            entries[1]["slot"], entries[0]["arrival_slot"] + 1
        )
        self.assertEqual(
            validate_tdm_schedule(routes, platform, schedule)["status"],
            "pass",
        )

    def test_multicast_cut_checks_every_sink_arrival_and_capture(self):
        ir = _wide_fanout_ir(width=2)
        platform = _three_fpga_platform("star")
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "q0": "fpga0",
            "source_lut": "fpga0",
            "sink_lut_000": "fpga1",
            "sink_ff_000": "fpga1",
            "sink_lut_001": "fpga2",
            "sink_ff_001": "fpga2",
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-multicast-static-exact-v1",
            seed=0,
        )
        route_constraints = normalize_route_constraints(
            None, platform, frame_slots=16
        )
        routes = route_system_native(
            assignment, platform, route_constraints
        )
        routes = {
            **routes,
            "semantic_contract": assignment["semantic_contract"],
        }
        self.assertEqual(len(routes["routes"]), 1)
        self.assertEqual(routes["routes"][0]["sinks"], ["fpga1", "fpga2"])
        self.assertEqual(len(routes["routes"][0]["tree_edges"]), 2)
        schedule = build_tdm_schedule(routes, platform)
        completion = schedule["demand_completions"][0]
        self.assertEqual(
            set(completion["sink_arrival_slots"]), {"fpga1", "fpga2"}
        )
        capture_records = schedule["schedule_dependency_certificate"][
            "capture_readiness"
        ]
        self.assertEqual(
            {item["fpga"] for item in capture_records}, {"fpga1", "fpga2"}
        )
        self.assertEqual(
            validate_tdm_schedule(routes, platform, schedule)["status"],
            "pass",
        )

    def test_unrelated_slow_level_one_demand_does_not_gate_ready_chain(self):
        value = self.ir.to_dict()
        value["design"]["name"] = "path_local_readiness"
        value["instances"].extend(
            [
                {"id": "qu", "type": "FDRE", "resources": {"ff": 1}},
                {"id": "lu0", "type": "LUT2", "resources": {"lut": 1}},
                {"id": "lu1", "type": "LUT2", "resources": {"lut": 1}},
                {"id": "quout", "type": "FDRE", "resources": {"ff": 1}},
            ]
        )
        value["nets"].extend(
            [
                {
                    "id": "qu_q",
                    "name": "qu_q",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("qu", "Q")],
                    "sinks": [_endpoint("lu0", "I0")],
                },
                {
                    "id": "u",
                    "name": "u",
                    "cut_class": "combinational",
                    "drivers": [_endpoint("lu0", "O")],
                    "sinks": [_endpoint("lu1", "I0")],
                },
                {
                    "id": "u_d",
                    "name": "u_d",
                    "cut_class": "register_input",
                    "drivers": [_endpoint("lu1", "O")],
                    "sinks": [_endpoint("quout", "D")],
                },
            ]
        )
        ir = EmuIR(value)
        platform = _three_fpga_platform("line")
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga1",
            "l2": "fpga0",
            "q1": "fpga0",
            "qu": "fpga0",
            "lu0": "fpga0",
            "lu1": "fpga2",
            "quout": "fpga2",
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-path-local-static-exact-v1",
            seed=0,
        )
        routes = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(None, platform, frame_slots=16),
        )
        routes = {
            **routes,
            "semantic_contract": assignment["semantic_contract"],
        }
        schedule = build_tdm_schedule(routes, platform)
        readiness = {
            item["net"]: item["source_ready_slot"]
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        completions = {
            item["net"]: item["completion_slot"]
            for item in schedule["demand_completions"]
        }
        self.assertLess(readiness["n1"], completions["u"])
        self.assertEqual(
            validate_tdm_schedule(routes, platform, schedule)["status"],
            "pass",
        )

    def test_wide_cone_splits_and_improves_checked_balance(self):
        ir = _wide_fanout_ir()
        constraints = normalize_partition_constraints(None, ir, self.platform)
        safe_clusters = build_clusters(ir, constraints)
        exact_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
        )
        self.assertEqual(
            max(len(item["instances"]) for item in safe_clusters["clusters"]),
            33,
        )
        self.assertEqual(
            max(len(item["instances"]) for item in exact_clusters["clusters"]),
            1,
        )
        safe_assignment = assign_clusters(
            ir, self.platform, safe_clusters, constraints, seed=9
        )
        exact_assignment = assign_clusters(
            ir, self.platform, exact_clusters, constraints, seed=9
        )
        with self.assertRaisesRegex(
            ValidationError, "multi-resource balance/capacity"
        ):
            validate_partition_artifacts(
                ir, self.platform, safe_clusters, safe_assignment
            )
        exact_validation = validate_partition_artifacts(
            ir, self.platform, exact_clusters, exact_assignment
        )
        self.assertEqual(exact_validation["effective_balance_percent"], 10.0)
        self.assertFalse(exact_validation["balance_auto_relaxed"])
        self.assertGreater(
            exact_validation["combinational_cut_nets"], 0
        )

    def test_phase3_cli_emits_opt_in_provisional_qualification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            output = root / "phase3"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        "phase3",
                        "--ir",
                        str(ir_path),
                        "--platform",
                        str(PLATFORM_PATH),
                        "--provider",
                        "greedy",
                        "--cut-mode",
                        "static-exact-combinational",
                        "--max-cross-fpga-dependency-depth",
                        "2",
                        "--balance-tolerance",
                        "1.0",
                        "--out",
                        str(output),
                    ]
                )
            self.assertEqual(status, 0)
            report = json.loads(
                (output / "phase3_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["qualification"],
                "structural-partition-legality",
            )
            self.assertEqual(
                report["validation"]["cut_mode"],
                "static-exact-combinational",
            )

    def test_phase4_binds_exact_contract_without_duplication(self):
        _, assignment = self._exact_artifacts()
        routes = self._persisted_routes(self._exact_routes(assignment))
        validation = validate_system_routes(assignment, self.platform, routes)
        self.assertEqual(validation["status"], "pass")
        self.assertNotIn("semantic_contract", routes)
        tampered = copy.deepcopy(routes)
        tampered["semantic_contract_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "semantic_contract"):
            validate_system_routes(assignment, self.platform, tampered)

    def test_phase5_uses_path_local_predecessor_readiness(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            schedule["provider"], TDM_EXACT_DEPENDENCY_PROVIDER
        )
        self.assertEqual(
            schedule["schedule_dependency_certificate"]["schema"],
            SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
        )
        validation = validate_tdm_schedule(
            routes, self.platform, schedule
        )
        self.assertEqual(
            validation["qualification"],
            "dependency-schedule-readiness-pass",
        )
        self.assertNotIn("transport_rounds", validation)
        self.assertNotIn("round_barriers", validation)
        self.assertNotIn("max_transport_round", validation)
        self.assertEqual(
            validation["transport_ordering_policy"],
            "path-local-dependency-dag-v1",
        )
        self.assertTrue(
            all(
                "transport_round" not in route
                for route in schedule["routes"]
            )
        )
        self.assertTrue(
            all(
                "transport_round" not in completion
                for completion in schedule["demand_completions"]
            )
        )
        readiness = {
            item["net"]: item
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        self.assertEqual(readiness["n0"]["source_ready_slot"], 1)
        self.assertEqual(readiness["d"]["source_ready_slot"], 4)
        self.assertEqual(
            readiness["d"]["evidence"][0]["predecessor_cut_net"],
            "n0",
        )
        tampered = copy.deepcopy(schedule)
        entry = next(item for item in tampered["entries"] if item["net"] == "d")
        entry["ready_slot"] -= 1
        with self.assertRaisesRegex(
            ValidationError, "ready-slot mismatch|inconsistent"
        ):
            validate_tdm_schedule(routes, self.platform, tampered)

        tampered = copy.deepcopy(schedule)
        tampered["entries"][0]["arrival_slot"] += 1
        with self.assertRaisesRegex(
            ValidationError, "arrival_slot|inconsistent"
        ):
            validate_tdm_schedule(routes, self.platform, tampered)

        tampered = copy.deepcopy(schedule)
        tampered["schedule_dependency_certificate"]["capture_readiness"][0][
            "ready_slot"
        ] += 1
        with self.assertRaisesRegex(ValidationError, "certificate"):
            validate_tdm_schedule(routes, self.platform, tampered)

    def test_static_exact_uses_the_same_ratio_aware_tdm_solver(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 100.0,
                "negative_slack_scale_ns": 100.0,
                "max_clock_period_ns": 100.0,
            },
            "compression": {"original_paths": 1, "compressed_paths": 1},
            "paths": [
                {
                    "path": "cross-layer-path",
                    "clock_domain": "clk",
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": 1.0,
                    "cut_nets": ["n0", "d"],
                    "cut_transitions": [
                        {"net": "n0", "from": "fpga0", "to": "fpga1"},
                        {"net": "d", "from": "fpga1", "to": "fpga0"},
                    ],
                }
            ],
        }
        plan = build_tdm_ratio_plan(
            routes,
            self.platform,
            executable=str(tdm_ratio_optimizer()),
            max_ratio=16,
            post_refinement_iterations=0,
        )
        schedule = build_tdm_schedule(routes, self.platform, plan)
        self.assertEqual(
            schedule["provider"], TDM_ACADEMIC_LIST_SCHEDULE_PROVIDER
        )
        self.assertEqual(
            schedule["transport_semantics"], "sampled-virtual-wire"
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule, plan)[
                "qualification"
            ],
            "dependency-schedule-readiness-pass",
        )
        refined = refine_tdm_schedule_native(
            routes,
            self.platform,
            plan,
            schedule,
            executable=str(tdm_slot_optimizer()),
            max_iterations=8,
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, refined, plan)[
                "qualification"
            ],
            "dependency-schedule-readiness-pass",
        )

    def test_register_output_launch_reserves_configured_settle_budget(self):
        """A cross-FPGA FF output cannot be sampled by TX in slot zero."""
        clusters = build_clusters(
            self.ir,
            self.constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            cluster_for["q0"]: "fpga0",
            cluster_for["l0"]: "fpga1",
            cluster_for["l1"]: "fpga1",
            cluster_for["l2"]: "fpga1",
            cluster_for["q1"]: "fpga1",
        }
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            self.constraints,
            targets,
            provider="test-static-exact-register-launch-v1",
            seed=0,
        )
        node = next(
            item
            for item in assignment["semantic_contract"]["cut_nodes"]
            if item["net"] == "q"
        )
        segments = {
            item["id"]: item
            for item in assignment["semantic_contract"]["logic_segments"]
        }
        launch = segments[node["source_segment_ids"][0]]
        self.assertEqual(launch["kind"], "launch_to_tx")
        self.assertNotIn("budget_slots", launch)

        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        readiness = next(
            item
            for item in schedule["schedule_dependency_certificate"]
            ["demand_readiness"]
            if item["net"] == "q"
        )
        self.assertEqual(readiness["source_ready_slot"], 1)
        self.assertGreaterEqual(
            min(item["slot"] for item in schedule["entries"] if item["net"] == "q"),
            1,
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_dependency_free_constant_cone_is_stable_at_slot_zero(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "constant_cut",
                    "top": "constant_cut",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {
                        "id": "constant_lut",
                        "type": "LUT2",
                        "parameters": {"INIT": "1111"},
                        "resources": {"lut": 1},
                    },
                    {
                        "id": "sink_lut",
                        "type": "LUT2",
                        "parameters": {"INIT": "1010"},
                        "resources": {"lut": 1},
                    },
                    {"id": "sink_ff", "type": "FDRE", "resources": {"ff": 1}},
                ],
                "nets": [
                    {
                        "id": "constant_cross",
                        "name": "constant_cross",
                        "cut_class": "combinational",
                        "drivers": [_endpoint("constant_lut", "O")],
                        "sinks": [_endpoint("sink_lut", "I0")],
                    },
                    {
                        "id": "d",
                        "name": "d",
                        "cut_class": "register_input",
                        "drivers": [_endpoint("sink_lut", "O")],
                        "sinks": [_endpoint("sink_ff", "D")],
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
        contract = build_static_exact_semantic_contract(
            ir,
            {"name": "test-platform"},
            {
                "constant_lut": "fpga0",
                "sink_lut": "fpga1",
                "sink_ff": "fpga1",
            },
            [
                {
                    "net": "constant_cross",
                    "source_fpgas": ["fpga0"],
                    "sink_fpgas": ["fpga1"],
                }
            ],
            max_dependency_depth=1,
            candidate_selection_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        launch = next(
            item
            for item in contract["logic_segments"]
            if item["kind"] == "launch_to_tx"
        )
        self.assertNotIn("budget_slots", launch)
        self.assertEqual(
            launch["source_semantics"], "configuration-stable-constant"
        )

    def test_phase5_cli_writes_and_revalidates_exact_schedule(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._persisted_routes(self._exact_routes(assignment))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            routes_path = root / "routes.json"
            assignment_path = root / "assignment.json"
            output = root / "phase5"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            assignment_path.write_text(
                json.dumps(assignment), encoding="utf-8"
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "phase5",
                            "--routes",
                            str(routes_path),
                            "--assignment",
                            str(assignment_path),
                            "--platform",
                            str(PLATFORM_PATH),
                            "--out",
                            str(output),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "schedule",
                            "validate",
                            str(output / "schedule.json"),
                            "--routes",
                            str(routes_path),
                            "--assignment",
                            str(assignment_path),
                            "--platform",
                            str(PLATFORM_PATH),
                        ]
                    ),
                    0,
                )
            report = json.loads(
                (output / "phase5_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["qualification"],
                "dependency-schedule-readiness-pass",
            )
            self.assertEqual(report["cut_mode"], "static-exact-combinational")
            schedule_manifest = json.loads(
                (output / "schedule.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["artifacts"]["schedule_store"],
                schedule_manifest["artifact"],
            )
            self.assertRegex(
                schedule_manifest["artifact"],
                r"^schedule\.[0-9a-f]{64}\.sqlite$",
            )
            self.assertTrue(
                (output / schedule_manifest["artifact"]).is_file()
            )

    def test_phase4_rejects_exact_contract_digest_tamper(self):
        _, assignment = self._exact_artifacts()
        routes = self._persisted_routes(self._exact_routes(assignment))
        tampered = copy.deepcopy(routes)
        tampered["semantic_contract_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "semantic_contract"):
            validate_system_routes(assignment, self.platform, tampered)

    def test_phase5_rejects_fixed_frame_that_cannot_meet_capture(self):
        _, assignment = self._exact_artifacts(
            frame_slots=4, dependent_return=True
        )
        routes = self._exact_routes(assignment, frame_slots=4)
        with self.assertRaisesRegex(ValidationError, "infeasible"):
            build_tdm_schedule(routes, self.platform)

    def test_phase6_event_model_uses_local_slot_values_and_is_equivalent(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        random_evidence = simulate_static_exact_partition_equivalence(
            self.ir,
            assignment,
            schedule,
            cycles=8,
            seed=19,
        )
        self.assertEqual(random_evidence["status"], "pass")
        self.assertEqual(
            random_evidence["evidence_type"], "random-simulation"
        )
        self.assertEqual(
            random_evidence["startup_uninitialized_shadow_reads"], 0
        )
        self.assertEqual(random_evidence["source_full_evaluations"], 0)
        self.assertEqual(random_evidence["reference_full_evaluations"], 8)
        self.assertEqual(
            random_evidence["initialization_full_evaluations"], 0
        )
        self.assertEqual(random_evidence["partition_full_evaluations"], 0)
        self.assertGreaterEqual(
            random_evidence["incremental_combinational_cell_evaluations"], 0
        )
        exhaustive = exhaustively_verify_static_exact_partition_equivalence(
            self.ir,
            assignment,
            schedule,
        )
        self.assertEqual(exhaustive["status"], "pass")
        self.assertEqual(exhaustive["evidence_type"], "exhaustive-small-model")
        self.assertEqual(exhaustive["cases"], 4)
        self.assertEqual(exhaustive["full_replay_cross_checks"], 4)
        self.assertEqual(exhaustive["source_full_evaluations"], 0)
        self.assertEqual(exhaustive["reference_full_evaluations"], 4)
        self.assertEqual(exhaustive["initialization_full_evaluations"], 0)
        self.assertEqual(exhaustive["partition_full_evaluations"], 0)

    def test_phase6_event_model_rejects_tx_before_local_source_ready(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        tampered = copy.deepcopy(schedule)
        first = next(item for item in tampered["entries"] if item["net"] == "n0")
        first["slot"] = 0
        first["ready_slot"] = 0
        with self.assertRaisesRegex(ValidationError, "before source-ready"):
            simulate_static_exact_partition_equivalence(
                self.ir,
                assignment,
                tampered,
                cycles=1,
            )

    def test_phase6_materializes_preserved_exact_boundaries_without_bypass(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        schedule = build_tdm_schedule(
            self._exact_routes(assignment), self.platform
        )
        artifacts = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        validation = validate_split_artifacts(
            self.ir,
            assignment,
            schedule,
            self.platform,
            artifacts,
        )
        self.assertEqual(validation["cut_mode"], "static-exact-combinational")
        self.assertEqual(validation["hidden_cross_fpga_bypass_errors"], 0)
        self.assertEqual(
            validation["exact_boundary_identities"],
            2 * len(schedule["entries"]),
        )
        boundaries = [
            endpoint["boundary_identity"]
            for transport in artifacts["transports"].values()
            for endpoint in transport["endpoints"]
        ]
        self.assertEqual(len(boundaries), len(set(boundaries)))
        for fpga_id, transport in artifacts["transports"].items():
            rtl = transport_to_systemverilog(transport, self.platform)
            self.assertIn('(* KEEP = "yes", DONT_TOUCH = "yes" *)', rtl)
            self.assertIn(fpga_id, artifacts["netlists"])

        tampered = copy.deepcopy(artifacts)
        shadow = next(
            segment
            for netlist in tampered["netlists"].values()
            for segment in netlist["nets"]
            if segment["source_kind"] == "transport_shadow"
        )
        shadow["drivers"] = [_endpoint("l0", "O")]
        with self.assertRaises(ValidationError):
            validate_split_artifacts(
                self.ir,
                assignment,
                schedule,
                self.platform,
                tampered,
            )

    def test_phase6_run_and_independent_validate_use_exact_macro_model(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        schedule = build_tdm_schedule(
            self._exact_routes(assignment), self.platform
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            schedule_path = root / "schedule.json"
            output = root / "phase6"
            self.ir.save(ir_path)
            assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
            write_phase3_assignment_index(
                root / "assignment.sqlite", self.ir, assignment
            )
            indexed_segments = []
            indexed_captures = []
            for fpga in ("fpga0", "fpga1"):
                partition = read_phase3_exact_semantic_partition(
                    root / "assignment.sqlite", fpga
                )
                self.assertEqual(
                    partition["sha256"],
                    assignment["semantic_contract_sha256"],
                )
                indexed_segments.extend(partition["logic_segments"])
                indexed_captures.extend(partition["capture_requirements"])
            self.assertEqual(
                sorted(indexed_segments, key=lambda item: item["id"]),
                assignment["semantic_contract"]["logic_segments"],
            )
            self.assertEqual(
                sorted(indexed_captures, key=lambda item: item["id"]),
                assignment["semantic_contract"]["capture_requirements"],
            )
            legacy_transport = write_transport_storage(
                root / "legacy-transport",
                root / "assignment.sqlite",
                schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            write_tdm_schedule_storage(schedule_path, schedule)
            indexed_schedule = read_tdm_schedule(schedule_path)
            typed_transport = write_transport_storage(
                root / "typed-transport",
                root / "assignment.sqlite",
                indexed_schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            self.assertEqual(
                typed_transport["lane_map"].materialize(),
                legacy_transport["lane_map"].materialize(),
            )
            self.assertEqual(
                {
                    fpga: value.materialize()
                    for fpga, value in typed_transport["transports"].items()
                },
                {
                    fpga: value.materialize()
                    for fpga, value in legacy_transport["transports"].items()
                },
            )
            self.assertEqual(
                {
                    fpga: value.materialize()
                    for fpga, value in typed_transport["anchors"].items()
                },
                {
                    fpga: value.materialize()
                    for fpga, value in legacy_transport["anchors"].items()
                },
            )
            report = run_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output,
                equivalence_cycles=4,
                equivalence_seed=31,
            )
            manifest = read_json(output / "manifest.json")
            self.assertEqual(
                manifest["semantic_contract_storage"]["artifact"],
                "assignment.exact.bin",
            )
            for fpga in ("fpga0", "fpga1"):
                transport = read_json(output / fpga / "transport.json")
                self.assertEqual(
                    transport["semantic_contract_storage"],
                    manifest["semantic_contract_storage"],
                )
                for endpoint in transport["endpoints"]:
                    self.assertNotIn("source_logic_segment_ids", endpoint)
                    self.assertNotIn("consumer_logic_segment_ids", endpoint)
            with patch(
                "emuflow.split_netlist_storage._exact_relation_maps",
                side_effect=AssertionError(
                    "bounded split metadata must not expand exact relations"
                ),
            ):
                with SplitNetlistView(
                    output / "split-netlists.sqlite", "fpga0"
                ) as split_view:
                    self.assertGreater(split_view.counts()["nets"], 0)
            evidence = report["equivalence"]
            self.assertEqual(evidence["status"], "pass")
            self.assertEqual(evidence["random_trace_count"], 3)
            self.assertEqual(evidence["random_macro_cycles"], 12)
            self.assertEqual(evidence["source_full_evaluations"], 0)
            self.assertEqual(evidence["reference_full_evaluations"], 12)
            self.assertEqual(evidence["initialization_full_evaluations"], 0)
            self.assertEqual(evidence["partition_full_evaluations"], 0)
            self.assertEqual(
                evidence["exhaustive_macro_step"]["evidence_type"],
                "exhaustive-small-model",
            )
            validation = validate_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output / "manifest.json",
                replay_equivalence=True,
            )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(
                validation["static_exact_equivalence"]["status"], "pass"
            )
            with patch(
                "emuflow.phase6._static_exact_equivalence_evidence",
                side_effect=AssertionError("equivalence replay was not reusable"),
            ):
                structural_validation = validate_phase6(
                    ir_path,
                    assignment_path,
                    schedule_path,
                    PLATFORM_PATH,
                    output / "manifest.json",
                    replay_equivalence=False,
                )
            self.assertEqual(structural_validation["status"], "pass")
            self.assertNotIn(
                "static_exact_equivalence", structural_validation
            )

    @unittest.skipUnless(shutil.which("yosys"), "yosys is not installed")
    def test_phase6_representative_depths_have_formal_macro_step_miters(self):
        fixture = (
            ROOT
            / "tests"
            / "fixtures"
            / "static_exact_macro_step_miter.sv"
        )
        for depth in (1, 2, 3):
            with self.subTest(depth=depth):
                command = (
                    f"read_verilog -formal -sv {fixture}; "
                    "prep -top "
                    f"static_exact_macro_step_depth{depth}_miter; "
                    "chformal -lower; "
                    "sat -verify -prove-asserts"
                )
                completed = subprocess.run(
                    [shutil.which("yosys"), "-q", "-p", command],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=completed.stdout + completed.stderr,
                )


if __name__ == "__main__":
    unittest.main()
