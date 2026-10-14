"""ASP-DAC 2026 equation-level continuous timing-DAG TDM optimizer."""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable
from .platform import Platform
from .routing import maximum_schedulable_tdm_ratio
from .tdm_ratio import (
    TDM_TIMING_DAG_RATIO_PROVIDER,
    _prepare_model,
    build_tdm_ratio_plan,
)


TDM_TIMING_DAG_PROVIDER = "aspdac26-timing-dag-equations-v1"
TDM_TIMING_DAG_SCHEMA = "emuflow.tdm-timing-dag-seed/v1"


def _build_dag(model: Mapping[str, Any]) -> Dict[str, Any]:
    """Build an exact prefix DAG for the compressed timing paths.

    Ratio-bearing edges are shared only when timing paths have the same
    prefix and routed hop. A unique fixed-delay edge terminates every path,
    so the global sink arrival is exactly the maximum enumerated path delay.
    """
    edges: List[Dict[str, Any]] = []
    prefix_edge: Dict[Tuple[int, int], Tuple[int, int]] = {}
    terminal_records = []
    next_node = 1
    hop_occurrences: Dict[int, List[int]] = defaultdict(list)
    for timing_path in model["timing_paths"]:
        node = 0
        member_edges = []
        for hop_index in timing_path["hops"]:
            key = (node, hop_index)
            if key in prefix_edge:
                edge_index, child = prefix_edge[key]
            else:
                child = next_node
                next_node += 1
                hop = model["hops"][hop_index]
                edge_index = len(edges)
                edges.append(
                    {
                        "index": edge_index,
                        "from": node,
                        "to": child,
                        "hop": hop_index,
                        "base_delay_ns": hop["base_delay_ns"],
                        "beta_ns": hop["beta_ns"],
                    }
                )
                prefix_edge[key] = (edge_index, child)
                hop_occurrences[hop_index].append(edge_index)
            member_edges.append(edge_index)
            node = child
        terminal_records.append(
            {
                "path": timing_path["index"],
                "from": node,
                "fixed_delay_ns": timing_path["fixed_delay_ns"],
                "member_edges": member_edges,
            }
        )
    sink = next_node
    paths = []
    for terminal in terminal_records:
        edge_index = len(edges)
        edges.append(
            {
                "index": edge_index,
                "from": terminal["from"],
                "to": sink,
                "hop": -1,
                "base_delay_ns": terminal["fixed_delay_ns"],
                "beta_ns": 0.0,
            }
        )
        paths.append(
            {
                "index": terminal["path"],
                "terminal_edge": edge_index,
                "member_edges": terminal["member_edges"],
            }
        )
    paths.sort(key=lambda item: item["index"])
    return {
        "source": 0,
        "sink": sink,
        "nodes": sink + 1,
        "edges": edges,
        "paths": paths,
        "hop_occurrences": {
            index: hop_occurrences.get(index, [])
            for index in range(len(model["hops"]))
        },
    }


def _write_native_input(
    path: Path,
    model: Mapping[str, Any],
    dag: Mapping[str, Any],
    *,
    max_iterations: int,
    min_ratio: float,
    max_ratio: float,
    convergence: float,
) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("EMUFLOW_TDM_TIMING_DAG_INPUT_V1\n")
        stream.write(
            f"PARAM {max_iterations} {min_ratio:.17g} {max_ratio:.17g} "
            f"{convergence:.17g} {dag['source']} {dag['sink']}\n"
        )
        stream.writelines(
            f"DOMAIN {domain['index']} {domain['lanes']:.17g}\n"
            for domain in model["domains"]
        )
        stream.writelines(
            f"HOP {hop['index']} {hop['domain']}\n"
            for hop in model["hops"]
        )
        stream.writelines(
            f"EDGE {edge['index']} {edge['from']} {edge['to']} "
            f"{edge['hop']} {edge['base_delay_ns']:.17g} "
            f"{edge['beta_ns']:.17g}\n"
            for edge in dag["edges"]
        )
        stream.writelines(
            f"PATH {timing_path['index']} "
            f"{timing_path['terminal_edge']}\n"
            for timing_path in dag["paths"]
        )


def _parse_native_output(
    path: Path,
    model: Mapping[str, Any],
    dag: Mapping[str, Any],
) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_TDM_TIMING_DAG_OUTPUT_V1":
        raise EmuFlowError("timing-DAG optimizer returned an invalid header")
    hops: Dict[int, float] = {}
    edges: Dict[int, float] = {}
    paths: Dict[int, float] = {}
    domains: Dict[int, Dict[str, float]] = {}
    metrics: Dict[str, Any] = {}
    integer_metrics = {"iterations", "residual_scalings"}
    for line in lines[1:]:
        fields = line.split()
        try:
            if len(fields) == 3 and fields[0] == "HOP":
                index = int(fields[1])
                if index in hops:
                    raise EmuFlowError("duplicate timing-DAG HOP output")
                hops[index] = float(fields[2])
            elif len(fields) == 3 and fields[0] == "EDGE":
                index = int(fields[1])
                if index in edges:
                    raise EmuFlowError("duplicate timing-DAG EDGE output")
                edges[index] = float(fields[2])
            elif len(fields) == 4 and fields[0] == "DOMAIN":
                index = int(fields[1])
                if index in domains:
                    raise EmuFlowError("duplicate timing-DAG DOMAIN output")
                domains[index] = {
                    "lambda": float(fields[2]),
                    "usage": float(fields[3]),
                }
            elif len(fields) == 3 and fields[0] == "PATH":
                index = int(fields[1])
                if index in paths:
                    raise EmuFlowError("duplicate timing-DAG PATH output")
                paths[index] = float(fields[2])
            elif len(fields) == 3 and fields[0] == "METRIC":
                key = fields[1]
                if key in metrics:
                    raise EmuFlowError("duplicate timing-DAG metric")
                metrics[key] = (
                    int(fields[2])
                    if key in integer_metrics
                    else float(fields[2])
                )
            else:
                raise EmuFlowError(
                    f"invalid timing-DAG output record: {line}"
                )
        except ValueError as error:
            raise EmuFlowError(
                f"malformed timing-DAG output record: {line}"
            ) from error
    if set(hops) != set(range(len(model["hops"]))):
        raise EmuFlowError("timing-DAG HOP coverage is not exact")
    if set(edges) != set(range(len(dag["edges"]))):
        raise EmuFlowError("timing-DAG EDGE coverage is not exact")
    if set(domains) != set(range(len(model["domains"]))):
        raise EmuFlowError("timing-DAG DOMAIN coverage is not exact")
    if set(paths) != set(range(len(model["timing_paths"]))):
        raise EmuFlowError("timing-DAG PATH coverage is not exact")
    expected_metrics = {
        "iterations",
        "residual_scalings",
        "sink_arrival_ns",
        "max_flow_conservation_error",
        "max_capacity_error",
    }
    if set(metrics) != expected_metrics:
        raise EmuFlowError("timing-DAG metric coverage is not exact")
    return {
        "ratios": [hops[index] for index in range(len(hops))],
        "edge_mu": [edges[index] for index in range(len(edges))],
        "path_mu": [paths[index] for index in range(len(paths))],
        "domains": [domains[index] for index in range(len(domains))],
        "metrics": metrics,
    }


def _topological_order(dag: Mapping[str, Any]) -> List[int]:
    outgoing: Dict[int, List[int]] = defaultdict(list)
    indegree = [0] * dag["nodes"]
    for edge in dag["edges"]:
        outgoing[edge["from"]].append(edge["to"])
        indegree[edge["to"]] += 1
    ready = deque(index for index, degree in enumerate(indegree) if degree == 0)
    order = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for sink in outgoing[node]:
            indegree[sink] -= 1
            if indegree[sink] == 0:
                ready.append(sink)
    if len(order) != dag["nodes"]:
        raise ValidationError("timing-DAG reconstruction found a cycle")
    return order


def _edge_delays(
    dag: Mapping[str, Any], ratios: List[float]
) -> List[float]:
    return [
        edge["base_delay_ns"]
        + (
            edge["beta_ns"] * (ratios[edge["hop"]] - 1.0)
            if edge["hop"] >= 0
            else 0.0
        )
        for edge in dag["edges"]
    ]


def _arrival_times(
    dag: Mapping[str, Any], edge_delays: List[float]
) -> List[float]:
    outgoing: Dict[int, List[int]] = defaultdict(list)
    for edge in dag["edges"]:
        outgoing[edge["from"]].append(edge["index"])
    arrivals = [float("-inf")] * dag["nodes"]
    arrivals[dag["source"]] = 0.0
    for node in _topological_order(dag):
        for index in outgoing[node]:
            edge = dag["edges"][index]
            arrivals[edge["to"]] = max(
                arrivals[edge["to"]], arrivals[node] + edge_delays[index]
            )
    return arrivals


def _expected_mu(
    dag: Mapping[str, Any],
    edge_delays: List[float],
    path_mu: List[float],
) -> Tuple[List[float], float]:
    incoming: Dict[int, List[int]] = defaultdict(list)
    outgoing: Dict[int, List[int]] = defaultdict(list)
    node_cost = [0.0] * dag["nodes"]
    edge_cost = [0.0] * len(dag["edges"])
    order = _topological_order(dag)
    for edge in dag["edges"]:
        incoming[edge["to"]].append(edge["index"])
        outgoing[edge["from"]].append(edge["index"])
    for node in order:
        split = node_cost[node] / len(outgoing[node]) if outgoing[node] else 0.0
        for index in outgoing[node]:
            edge = dag["edges"][index]
            edge_cost[index] = max(split + edge_delays[index], 1.0e-12)
            node_cost[edge["to"]] += edge_cost[index]
    mu = [0.0] * len(dag["edges"])
    for timing_path, multiplier in zip(dag["paths"], path_mu):
        mu[timing_path["terminal_edge"]] = multiplier
    for node in reversed(order):
        if node == dag["sink"]:
            continue
        outflow = sum(mu[index] for index in outgoing[node])
        if not incoming[node]:
            continue
        denominator = node_cost[node]
        if denominator <= 0.0:
            share = outflow / len(incoming[node])
            for index in incoming[node]:
                mu[index] = share
        else:
            for index in incoming[node]:
                mu[index] = outflow * edge_cost[index] / denominator
    maximum_error = 0.0
    for node in order:
        if node in {dag["source"], dag["sink"]}:
            continue
        maximum_error = max(
            maximum_error,
            abs(
                sum(mu[index] for index in incoming[node])
                - sum(mu[index] for index in outgoing[node])
            ),
        )
    return mu, maximum_error


def validate_timing_dag_seed(
    model: Mapping[str, Any],
    dag: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    if result.get("schema") != TDM_TIMING_DAG_SCHEMA:
        raise ValidationError("timing-DAG seed schema is invalid")
    if result.get("provider") != TDM_TIMING_DAG_PROVIDER:
        raise ValidationError("timing-DAG seed provider is invalid")
    configuration = result.get("configuration")
    ratios = result.get("continuous_ratios")
    edge_mu = result.get("edge_mu")
    path_mu = result.get("path_mu")
    if (
        not isinstance(configuration, dict)
        or not isinstance(ratios, list)
        or len(ratios) != len(model["hops"])
        or not isinstance(edge_mu, list)
        or len(edge_mu) != len(dag["edges"])
        or not isinstance(path_mu, list)
        or len(path_mu) != len(model["timing_paths"])
    ):
        raise ValidationError("timing-DAG seed coverage is incomplete")
    maximum_ratio = configuration.get("max_ratio")
    minimum_ratio = configuration.get("min_ratio")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum_ratio - 1.0e-10 <= value <= maximum_ratio + 1.0e-10
        for value in ratios
    ):
        raise ValidationError("timing-DAG seed contains an invalid ratio")
    delays = _edge_delays(dag, ratios)
    arrivals = _arrival_times(dag, delays)
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0.0
            for value in path_mu
        )
        or not math.isclose(sum(path_mu), 1.0, rel_tol=1.0e-9, abs_tol=1.0e-9)
    ):
        raise ValidationError("timing-DAG path multipliers violate Eq. 15")
    expected_mu, conservation_error = _expected_mu(dag, delays, path_mu)
    if any(
        not math.isclose(actual, expected, rel_tol=1.0e-9, abs_tol=1.0e-9)
        for actual, expected in zip(edge_mu, expected_mu)
    ):
        raise ValidationError("timing-DAG Eq. 16/17 multiplier mismatch")
    path_delays = []
    for path, timing_path in zip(dag["paths"], model["timing_paths"]):
        path_delay = timing_path["fixed_delay_ns"] + sum(
            model["hops"][hop]["base_delay_ns"]
            + model["hops"][hop]["beta_ns"] * (ratios[hop] - 1.0)
            for hop in timing_path["hops"]
        )
        terminal = dag["edges"][path["terminal_edge"]]
        reconstructed = arrivals[terminal["from"]] + delays[path["terminal_edge"]]
        if not math.isclose(
            path_delay, reconstructed, rel_tol=1.0e-10, abs_tol=1.0e-10
        ):
            raise ValidationError("timing-DAG path reconstruction mismatch")
        path_delays.append(path_delay)
    worst_delay = max(path_delays)
    metrics = result.get("metrics")
    if (
        not isinstance(metrics, dict)
        or not math.isclose(
            arrivals[dag["sink"]], worst_delay, rel_tol=1.0e-10, abs_tol=1.0e-10
        )
        or not math.isclose(
            metrics.get("sink_arrival_ns", float("nan")),
            worst_delay,
            rel_tol=1.0e-10,
            abs_tol=1.0e-10,
        )
        or not math.isclose(
            metrics.get("max_flow_conservation_error", float("nan")),
            conservation_error,
            rel_tol=1.0e-9,
            abs_tol=1.0e-9,
        )
    ):
        raise ValidationError("timing-DAG arrival/flow metrics mismatch")
    usages = [0.0] * len(model["domains"])
    for hop in model["hops"]:
        usages[hop["domain"]] += 1.0 / ratios[hop["index"]]
    for domain in model["domains"]:
        usage = usages[domain["index"]]
        if usage > domain["lanes"] + 1.0e-8:
            raise ValidationError("timing-DAG ratio capacity is exceeded")
    return {
        "status": "pass",
        "nodes": dag["nodes"],
        "edges": len(dag["edges"]),
        "covered_hops": sum(bool(value) for value in dag["hop_occurrences"].values()),
        "uncovered_hops": sum(not value for value in dag["hop_occurrences"].values()),
        "timing_paths": len(path_delays),
        "worst_delay_ns": worst_delay,
        "maximum_domain_usage": max(usages),
        "maximum_flow_conservation_error": conservation_error,
    }


def optimize_prepared_timing_dag(
    model: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    max_iterations: int = 500,
    min_ratio: float = 1.0,
    max_ratio: float = 32.0,
    convergence: float = 1.0e-9,
) -> Dict[str, Any]:
    for name, value in (
        ("max_iterations", max_iterations),
        ("min_ratio", min_ratio),
        ("max_ratio", max_ratio),
        ("convergence", convergence),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValidationError(f"timing-DAG {name} is invalid")
    if (
        max_iterations != int(max_iterations)
        or min_ratio < 1.0
        or max_ratio < min_ratio
    ):
        raise ValidationError("timing-DAG iteration/ratio bounds are invalid")
    dag = _build_dag(model)
    resolved = resolve_native_executable(
        "emuflow_tdm_timing_dag_optimizer", executable
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-tdm-dag-") as temporary:
        root = Path(temporary)
        native_input = root / "timing-dag.in"
        native_output = root / "timing-dag.out"
        _write_native_input(
            native_input,
            model,
            dag,
            max_iterations=int(max_iterations),
            min_ratio=float(min_ratio),
            max_ratio=float(max_ratio),
            convergence=float(convergence),
        )
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise EmuFlowError(
                "in-tree timing-DAG optimizer failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        native = _parse_native_output(native_output, model, dag)
    result = {
        "schema": TDM_TIMING_DAG_SCHEMA,
        "provider": TDM_TIMING_DAG_PROVIDER,
        "equations": [8, 13, 16, 17, 19, 20],
        "paper": {
            "title": (
                "Timing-Aware Optimization of Die-Level Routing and TDM "
                "Assignment for Multi-FPGA Systems"
            ),
            "venue": "ASP-DAC 2026",
        },
        "configuration": {
            "max_iterations": int(max_iterations),
            "min_ratio": float(min_ratio),
            "max_ratio": float(max_ratio),
            "convergence": float(convergence),
        },
        "dag": {
            "source": dag["source"],
            "sink": dag["sink"],
            "nodes": dag["nodes"],
            "edges": len(dag["edges"]),
            "hop_occurrences": dag["hop_occurrences"],
        },
        "continuous_ratios": native["ratios"],
        "edge_mu": native["edge_mu"],
        "path_mu": native["path_mu"],
        "domains": native["domains"],
        "metrics": native["metrics"],
    }
    result["validation"] = validate_timing_dag_seed(model, dag, result)
    return result


def build_timing_dag_ratio_seed(
    routes: Mapping[str, Any],
    platform: Platform,
    *,
    executable: Optional[str] = None,
    max_iterations: int = 500,
    min_ratio: float = 1.0,
    max_ratio: float = 32.0,
    convergence: float = 1.0e-9,
) -> Dict[str, Any]:
    model = _prepare_model(routes, platform)
    return optimize_prepared_timing_dag(
        model,
        executable=executable,
        max_iterations=max_iterations,
        min_ratio=min_ratio,
        max_ratio=max_ratio,
        convergence=convergence,
    )


def build_timing_dag_ratio_plan(
    routes: Mapping[str, Any],
    platform: Platform,
    *,
    dag_executable: Optional[str] = None,
    legalization_executable: Optional[str] = None,
    max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: Optional[int] = None,
    post_refinement_iterations: int = 200,
    exact_domain_limit: int = 2048,
    convergence: float = 1.0e-9,
    prepared_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    model = (
        prepared_model
        if prepared_model is not None
        else _prepare_model(routes, platform)
    )
    min_ratio = int(model["constraints"].get("tdm_min_ratio", 1))
    if ratio_quantum is None:
        ratio_quantum = int(
            model["constraints"].get("tdm_ratio_quantum", 8)
        )
    if max_ratio is None:
        link_by_id = {link.id: link for link in platform.links}
        max_ratio = min(
            maximum_schedulable_tdm_ratio(
                model["constraints"],
                link_by_id[hop["link"]].latency_cycles,
                ratio_quantum=ratio_quantum,
                minimum_ratio=min_ratio,
            )
            for hop in model["hops"]
        )
    seed = optimize_prepared_timing_dag(
        model,
        executable=dag_executable,
        max_iterations=max_iterations,
        min_ratio=float(min_ratio),
        max_ratio=max_ratio,
        convergence=convergence,
    )
    evidence = {
        "provider": seed["provider"],
        "equations": seed["equations"],
        "paper": seed["paper"],
        "metrics": seed["metrics"],
        "validation": seed["validation"],
    }
    return build_tdm_ratio_plan(
        routes,
        platform,
        executable=legalization_executable,
        max_iterations=max_iterations,
        max_ratio=max_ratio,
        ratio_quantum=ratio_quantum,
        post_refinement_iterations=post_refinement_iterations,
        exact_domain_limit=exact_domain_limit,
        convergence=convergence,
        continuous_seed=seed["continuous_ratios"],
        provider=TDM_TIMING_DAG_RATIO_PROVIDER,
        provider_metadata=evidence,
        prepared_model=model,
    )
