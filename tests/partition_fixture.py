"""Bounded initializer oracle for contract fixtures, never a production flow.

Generate a legal small .part file and exercise the real TritonPart import and
shared phase pipeline. NVDLA acceptance must use algorithm-selected native
initialization and cannot use this fixture.
"""
from pathlib import Path
import tempfile


def write_fixture_solution(ir, platform_path, path, *, cut_mode="sequential-only",
                           seed=0, min_used_fpgas=None, balance_tolerance=None,
                           constraints_path=None, max_cross_fpga_dependency_depth=0,
                           static_exact_candidate_policy="assignment-derived-acyclic-v2"):
    from tests.native_partition_facts import (build_clusters, load_partition_constraints)
    from tests.partition_reference import assign_clusters
    from emuflow.platform.platform import Platform
    if len(ir.value["instances"]) > 256:
        raise AssertionError("partition contract fixture exceeds its small-design bound")
    platform = Platform.load(platform_path)
    constraints = load_partition_constraints(constraints_path, ir, platform,
        min_used_fpgas=min_used_fpgas, balance_tolerance=balance_tolerance)
    clusters = build_clusters(ir, constraints, cut_mode=cut_mode,
        max_cross_fpga_dependency_depth=max_cross_fpga_dependency_depth,
        static_exact_candidate_policy=static_exact_candidate_policy)
    initial = assign_clusters(ir, platform, clusters, constraints, seed=seed,
                              _include_semantic_contract=False)
    parts = sorted(constraints["active_fpgas"])
    Path(path).write_text("\n".join(str(parts.index(initial["cluster_assignment"][c["id"]]))
                                     for c in clusters["clusters"]) + "\n")


def run_phase3_fixture(*args, **kwargs):
    import inspect
    from emuflow.partition.phase3 import run_phase3
    from emuflow.partition.partition_ir import load_partition_ir
    bound = inspect.signature(run_phase3).bind(*args, **kwargs)
    bound.apply_defaults()
    options = bound.arguments
    assert options["provider"] == "tritonpart"
    with tempfile.TemporaryDirectory(prefix="partition-contract-initial-") as temporary:
        solution = Path(temporary) / "initial.part"
        write_fixture_solution(load_partition_ir(options["ir_path"]), options["platform_path"], solution,
            **{name: options[name] for name in ("cut_mode", "seed", "min_used_fpgas", "balance_tolerance",
                "constraints_path", "max_cross_fpga_dependency_depth", "static_exact_candidate_policy")})
        options["tritonpart_solution"] = solution
        return run_phase3(**options)


def run_multi_fpga_flow_fixture(*args, **kwargs):
    """Exercise the actual native controller with a bounded initializer input."""
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    from tests.yosys_reference import import_yosys_json
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH
    assert not args and kwargs["partition_provider"] == "tritonpart"
    assert kwargs.get("yosys_json") is not None
    with tempfile.TemporaryDirectory(prefix="native-compile-fixture-") as directory:
        root = Path(directory)
        if kwargs.get("architecture_timing_db") is None:
            kwargs["architecture_timing_db"] = write_mock_xilinx_timing_contract(root / "timing.json")
        ir = import_yosys_json(kwargs["yosys_json"], top=kwargs.get("top"), clocks=kwargs.get("clocks", ()))
        solution = root / "initial.part"
        write_fixture_solution(ir, kwargs.get("platform_path", DEFAULT_PLATFORM_PATH), solution,
            constraints_path=kwargs.get("partition_constraints"), seed=kwargs.get("seed",0),
            cut_mode=kwargs.get("cut_mode", "static-exact-combinational"),
            min_used_fpgas=kwargs.get("min_used_fpgas"), balance_tolerance=kwargs.get("balance_tolerance"),
            max_cross_fpga_dependency_depth=kwargs.get("max_cross_fpga_dependency_depth",0),
            static_exact_candidate_policy=kwargs.get("static_exact_candidate_policy","assignment-derived-acyclic-v2"))
        return run_multi_fpga_flow(**dict(kwargs, tritonpart_solution=solution))
