"""Bounded initializer oracle for contract fixtures, never a production flow.

Generate a legal small .part file and exercise the real TritonPart import and
shared phase pipeline. NVDLA acceptance must use algorithm-selected native
initialization and cannot use this fixture.
"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch


def write_fixture_solution(ir, platform_path, path, *, cut_mode="sequential-only",
                           seed=0, min_used_fpgas=None, balance_tolerance=None,
                           constraints_path=None, max_cross_fpga_dependency_depth=0,
                           static_exact_candidate_policy="assignment-derived-acyclic-v2"):
    from emuflow.partition import (build_clusters, load_partition_constraints)
    from tests.partition_reference import assign_clusters
    from emuflow.platform import Platform
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
    from emuflow.phase3 import run_phase3
    from emuflow.partition_ir import load_partition_ir
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


@contextmanager
def _fixture_initializer(target):
    import importlib
    module = importlib.import_module(target)
    if isinstance(module.run_phase3, Mock):
        yield
    else:
        with patch(target + ".run_phase3", side_effect=run_phase3_fixture):
            yield


def run_multi_fpga_flow_fixture(*args, **kwargs):
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    assert kwargs["partition_provider"] == "tritonpart"
    # The .part file is generated at the phase boundary from the actual
    # frontend artifact. This unit fixture never starts OpenROAD.
    with _fixture_initializer("emuflow.multi_fpga_flow"), patch(
        "emuflow.multi_fpga_flow._resolve_partition_openroad", return_value=None
    ):
        return run_multi_fpga_flow(*args, **kwargs)
