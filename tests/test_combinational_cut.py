from tests.phase3_exact_fixture import write_exact_fixture
from tests.phase3_store_fixture import assignment_selector_fixture, cluster_document_fixture, write_assignment_index_fixture, write_current_assignment_fixture
from tests.native_split_fixture import NativeSplitFixture
from tests.native_route_fixture import route_system_native
from tests.schedule_store_fixture import write_tdm_schedule_storage
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

from emuflow_tools.stage_clients.partition import run_phase3
from emuflow_tools.stage_clients.schedule import run_phase5, validate_phase5
from emuflow.partition.combinational_cut import (STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2, STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA, semantic_contract_sha256)
from tests.native_partition_facts import semantic_contract_facts as build_static_exact_semantic_contract, candidate_index_facts as _build_combinational_cut_candidate_index
from emuflow.common.errors import ValidationError
from emuflow.validation.equivalence import (
    _MappedModel,
    exhaustively_verify_static_exact_partition_equivalence,
    simulate_static_exact_partition_equivalence,
)
from emuflow.ir.emuir_compact import write_compact_emuir
from emuflow.ir.ir import EmuIR
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow_tools.partition.partition_ir import load_partition_ir
from emuflow.partition.partition import (CUT_MODE_STATIC_EXACT)
from tests.native_partition_facts import (build_clusters, build_partition_assignment, normalize_partition_constraints, validate_partition_artifacts)
from tests.partition_reference import assign_clusters
from emuflow.platform.platform import Platform
from emuflow_tools.stage_clients.transport import run_phase6, validate_phase6
from emuflow.transport.split_netlist_storage import SplitNetlistView
from emuflow.partition.phase3_assignment_index import (ASSIGNMENT_INDEX_APPLICATION_ID, open_phase3_assignment_index, phase3_exact_semantic_counts, phase3_exact_relation_counts, read_phase3_exact_split_relations, validate_phase3_assignment_index)
from tests.native_semantic_facts import read_nodes as read_native_static_exact_cut_node_stream
from emuflow.partition.phase3_exact_binary import (validate_phase3_exact_binary)
from tests.phase3_exact_fixture import indexed_exact_facts
from emuflow.partition.phase3_storage import (PACKED_ASSIGNMENT_SCHEMA, read_phase3_assignment_view)
from emuflow.routing.routing import build_directed_graph, normalize_route_constraints
from tests.routing_validation_reference import demands_from_assignment, validate_system_routes
from emuflow.ir.resources import ResourceVector
from emuflow.scheduling.tdm import TDM_EXACT_DEPENDENCY_PROVIDER, SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA
from tests.tdm_reference import validate_tdm_schedule
from tests.schedule_reference import (
    REFERENCE_TDM_PROVIDER as TDM_BASELINE_PROVIDER,
    build_tdm_schedule,
)
from emuflow.scheduling.tdm_schedule_storage import (read_tdm_schedule)
from emuflow_tools.transport.transport_storage import write_transport_storage
from emuflow_tools.routing.system_route_storage import SystemRoutesView, read_system_routes
from tests.native_route_seal_fixture import write_route_store_fixture
from tests.native_build import emuflow_native, static_exact_analyzer, tlr_router


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
            "emuflow.partition.combinational_cut.json.dumps",
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














class StaticExactCombinationalCutPartitionTest(unittest.TestCase):
    def setUp(self):
        self.ir = _chain_ir()
        self.platform = Platform.load(PLATFORM_PATH)
        self.constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )







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
                        [executable, "phase3", "analyze", native_input, native_output],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("count is invalid", completed.stderr)
                    self.assertFalse(native_output.exists())














    def test_current_assignment_reader_preserves_lazy_semantic_reference(self):
        clusters, assignment = self._exact_artifacts(dependent_return=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index_summary = write_assignment_index_fixture(
                root / "assignment.sqlite", self.ir, assignment
            )
            packed = assignment_selector_fixture(
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
                cluster_document_fixture(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            loaded = read_json(root / "assignment.json")
            self.assertEqual(list(loaded['cut_nets']), assignment['cut_nets'])
            self.assertNotIn('semantic_contract', loaded)
            self.assertNotIn('cluster_assignment', loaded)
            self.assertNotIn('instance_assignment', loaded)

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
                ValidationError, "assignment cut identity disagrees"
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
            summary = write_assignment_index_fixture(
                root / "assignment.sqlite", self.ir, assignment
            )
            self.assertEqual(summary["cut_records"], 0)
            packed = assignment_selector_fixture(
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
                cluster_document_fixture(clusters),
                compact=True,
            )
            write_json(root / "assignment.json", packed, compact=True)
            view = read_phase3_assignment_view(root / "assignment.json")
            self.assertEqual(len(view["cut_nets"]), 0)
            self.assertEqual(list(view["cut_nets"]), [])
            loaded = read_json(root / "assignment.json")
            self.assertEqual(list(loaded['cut_nets']), assignment['cut_nets'])
            self.assertNotIn('semantic_contract', loaded)
            self.assertNotIn('cluster_assignment', loaded)
            self.assertNotIn('instance_assignment', loaded)

    def _exact_assignment_with_bit(self, bit):
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







    def test_current_assignment_binary_preserves_capture_bit_boundaries(self):
        for bit in ((1 << 63) - 1, 1 << 63, 1 << 8191):
            with self.subTest(bit_length=bit.bit_length()):
                assignment, capture_id, fpga = self._exact_assignment_with_bit(bit)
                with tempfile.TemporaryDirectory() as temporary:
                    index = Path(temporary) / "assignment.sqlite"
                    summary = write_assignment_index_fixture(index, self.ir, assignment)
                    self.assertEqual(summary["status"], "pass")
                    partition = indexed_exact_facts(index, fpga=fpga)
                    capture = next(r for r in partition["capture_requirements"] if r["id"] == capture_id)
                    self.assertEqual(capture["bit"], bit)
                    self.assertEqual(indexed_exact_facts(index, assignment), assignment["semantic_contract"])
                    self.assertEqual(validate_phase3_assignment_index(index)["status"], "pass")

    def test_current_assignment_rejects_oversized_capture_without_publishing(self):
        assignment, _, _ = self._exact_assignment_with_bit(0)
        capture = next(r for r in assignment["semantic_contract"]["capture_requirements"] if r["kind"] == "architectural-state")
        capture["bit"] = 1 << 8192
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "assignment.sqlite"
            with self.assertRaisesRegex(ValidationError, "at most 8192 bits"):
                write_assignment_index_fixture(index, self.ir, assignment)
            self.assertEqual(list(index.parent.iterdir()), [])

    def test_retired_assignment_versions_rejected_by_all_readers(self):
        from emuflow.partition.phase3_storage import (
            IndexedInstanceAssignment,
            read_phase3_cut_timing_view,
        )
        from emuflow.partition import phase3_assignment_index

        self.assertFalse(hasattr(phase3_assignment_index, "migrate_phase3_assignment_index"))
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "assignment.sqlite"
            (index.parent / "assignment.json").write_text("{}")
            for version in (1, 2):
                index.unlink(missing_ok=True)
                with closing(sqlite3.connect(index)) as db, db:
                    db.executescript(f"PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID}; PRAGMA user_version={version};")
                for reader in (
                    lambda: open_phase3_assignment_index(index),
                    lambda: IndexedInstanceAssignment(index),
                    lambda: read_phase3_cut_timing_view(index.parent / "assignment.json"),
                    lambda: phase3_exact_semantic_counts(index),
                    lambda: phase3_exact_relation_counts(index),
                    lambda: read_phase3_exact_split_relations(index),
                    lambda: validate_phase3_assignment_index(index),
                ):
                    with self.subTest(version=version, reader=reader), self.assertRaisesRegex(ValidationError, "unsupported Phase 3 assignment index"):
                        reader()

    def test_indexed_assignment_binary_retains_owned_exact_digest(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            summary = write_assignment_index_fixture(
                Path(temporary_directory) / "assignment.sqlite",
                self.ir,
                assignment,
            )
            self.assertEqual(summary["semantic_contract_sha256"], assignment["semantic_contract_sha256"])
            self.assertEqual(summary["semantic_binary"]["semantic_contract_sha256"], assignment["semantic_contract_sha256"])

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
        from tests.native_build import emuflow_native
        from emuflow.scheduling.tdm import build_native_exact_schedule_request

        def compare(candidate):
            reference = build_tdm_schedule(candidate, self.platform)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                route_path = root / "routes.json"
                value = dict(candidate)
                contract = value.pop("semantic_contract")
                write_route_store_fixture(route_path, value)
                native_routes = read_system_routes(route_path)
                native_routes["semantic_contract"] = contract
                request = build_native_exact_schedule_request(native_routes, self.platform)
                self.assertIsNotNone(request)
                path = root / "schedule.json"
                write_tdm_schedule_storage(path, request)
                self.assertEqual(read_tdm_schedule(path, materialize=True), reference)
                with read_tdm_schedule(path) as schedule, patch(
                    'tests.tdm_reference._indexed_exact_nodes', side_effect=AssertionError("R-sized node map")), patch(
                    'tests.tdm_reference._exact_dependency_order', side_effect=AssertionError("R-sized order")):
                    self.assertEqual(validate_tdm_schedule(native_routes, self.platform, schedule)["status"], "pass")

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
            write_route_store_fixture(route_path, repeated)
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
        implicit = build_clusters(self.ir, self.constraints, cut_mode="sequential-only")
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
        safe = build_clusters(self.ir, self.constraints, cut_mode="sequential-only")
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
        with NativeSplitFixture(self.ir, assignment, schedule, self.platform) as produced:
            self.assertEqual(produced.validate()['status'], 'pass')
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
        with NativeSplitFixture(self.ir, assignment, schedule, self.platform) as produced:
            self.assertEqual(produced.validate()['status'], 'pass')
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
        safe_clusters = build_clusters(ir, constraints, cut_mode="sequential-only")
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

    def test_native_phase3_emits_structural_qualification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            output = root / "phase3"
            write_compact_emuir(ir_path, self.ir.to_dict())
            from tests.partition_fixture import write_fixture_solution
            solution = root / "initial.part"
            write_fixture_solution(self.ir, PLATFORM_PATH, solution,
                cut_mode="static-exact-combinational",
                max_cross_fpga_dependency_depth=2, balance_tolerance=1.0)
            result = run_phase3(
                ir_path, PLATFORM_PATH, output, provider="tritonpart",
                tritonpart_solution=solution, cut_mode="static-exact-combinational",
                max_cross_fpga_dependency_depth=2, balance_tolerance=1.0,
            )
            self.assertEqual(result["status"], "pass")
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
            self.platform,
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

    def test_native_phase5_writes_and_revalidates_exact_schedule(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._persisted_routes(self._exact_routes(assignment))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            routes_path = root / "routes.json"
            assignment_path = root / "assignment.json"
            output = root / "phase5"
            from tests.native_route_seal_fixture import write_route_store_fixture
            write_route_store_fixture(routes_path, routes)
            write_current_assignment_fixture(assignment_path, self.ir, assignment)
            result = run_phase5(routes_path, PLATFORM_PATH, output, assignment_path=assignment_path)
            self.assertEqual(result["status"], "pass")
            validation = validate_phase5(
                routes_path, PLATFORM_PATH, output / "schedule.json", assignment_path=assignment_path,
            )
            self.assertEqual(validation["status"], "pass")
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
        schedule = build_tdm_schedule(self._exact_routes(assignment), self.platform)
        with NativeSplitFixture(self.ir, assignment, schedule, self.platform) as produced:
            artifacts = produced.artifacts()
            validation = produced.validate()
            self.assertEqual(artifacts['manifest']['cut_mode'], 'static-exact-combinational')
            self.assertEqual(validation['shadow_driver_errors'], 0)
            boundaries = [endpoint['boundary_identity'] for transport in artifacts['transports'].values() for endpoint in transport['endpoints']]
            self.assertEqual(len(boundaries), 2 * len(schedule['entries']))
            self.assertEqual(len(boundaries), len(set(boundaries)))
            for fpga, transport in artifacts['transports'].items():
                rtl = (produced.output / fpga / 'transport_schedule.sv').read_text()
                self.assertIn('(* KEEP = "yes", DONT_TOUCH = "yes" *)', rtl)
                self.assertIn(fpga, artifacts['netlists'])
            shadows = [net for netlist in artifacts['netlists'].values() for net in netlist['nets'] if net['source_kind'] == 'transport_shadow']
            self.assertTrue(shadows)
            for net in shadows:
                self.assertEqual(len(net['drivers']), 1)
                self.assertTrue(net['drivers'][0]['instance'].startswith('__emuflow_rx_'))
                self.assertEqual(net['drivers'][0]['port'], 'shadow_out')
            with closing(sqlite3.connect(produced.split)) as db, db:
                self.assertGreater(db.execute('SELECT count(*) FROM shadow_drivers').fetchone()[0], 0)
                db.execute('DELETE FROM shadow_drivers WHERE schedule_entry_seq IN (SELECT schedule_entry_seq FROM shadow_drivers LIMIT 1)')
            with self.assertRaises(ValidationError):
                produced.validate_split()

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
            write_current_assignment_fixture(assignment_path, self.ir, assignment)
            indexed_segments = []
            indexed_captures = []
            for fpga in ("fpga0", "fpga1"):
                partition = indexed_exact_facts(root / "assignment.sqlite", fpga=fpga)
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
            from tests.transport_reference import write_transport_storage as reference_transport
            legacy_transport = reference_transport(
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
                "emuflow.transport.split_netlist_storage._exact_relation_maps",
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
                "emuflow_tools.stage_clients.transport._static_exact_equivalence_evidence",
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
        schedule = build_tdm_schedule(hydrated, self.platform)
        self.assertEqual(schedule["transport_semantics"], "sampled-virtual-wire")
        self.assertNotIn("round_realization", schedule)
        self.assertEqual(validate_tdm_schedule(hydrated, self.platform, schedule)["status"], "pass")


if __name__ == "__main__":
    unittest.main()
