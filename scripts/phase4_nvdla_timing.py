#!/usr/bin/env python3
"""Evaluate every original Phase5 timing constraint, independently of compression.

The producer remains immutable. This evaluator adds each original path's own
logic delay to concrete ratio-based transport, then selects its worst output
copy. These are whole-population preplacement estimates, not physical signoff.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def close(a, b):
    return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=1e-9)


def population_digest(ids):
    return hashlib.sha256(json.dumps(sorted(ids), ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def path_transitions(path, cuts, owners, target=None):
    """Walk capture-to-launch ownership, excluding local fanouts of cut nets."""
    endpoint = path.get("endpoint")
    startpoint = path.get("startpoint")
    require(isinstance(endpoint, dict) and isinstance(startpoint, dict), "original endpoint identity missing")
    if target is None:
        target = owners.get(endpoint.get("instance"))
    require(isinstance(target, str) and target, "capture ownership missing")
    result = []
    for net in reversed(path["path_nets"]):
        cut = cuts.get(net)
        if cut is None:
            continue
        sources = cut["source_fpgas"]
        require(len(sources) == 1, "multiple cut sources unsupported")
        source = sources[0]
        if source == target:
            continue
        require(target in cut["sink_fpgas"], "broken original cut chain")
        result.append({"net": net, "from": source, "to": target})
        target = source
    start = startpoint.get("instance")
    if start is not None:
        require(owners.get(start) == target, "launch ownership disagrees with cut chain")
    return list(reversed(result))


def member_transport(routes, schedule, board):
    """Reconstruct linear-ratio delay from raw trees and actual schedule ratios."""
    links = {link["id"]: link for link in board["links"]}
    entries = {}
    for entry in schedule["entries"]:
        key = (entry["demand"], entry["link"], entry["from"], entry["to"])
        require(key not in entries, "duplicate schedule hop")
        entries[key] = entry
    by_net = {}
    used = set()
    for route in routes["routes"]:
        require(route["net"] not in by_net, "duplicate routed net")
        adjacency = defaultdict(list)
        for edge in route["tree_edges"]:
            adjacency[edge["from"]].append(edge)
        delays = {route["source"]: 0.0}
        pending = [route["source"]]
        while pending:
            node = pending.pop()
            for edge in adjacency[node]:
                sink = edge["to"]
                require(sink not in delays, "route is not a rooted tree")
                link = links[edge["link"]]
                require({node, sink} == set(link["endpoints"]), "route edge endpoints differ from BoardDB")
                model = link.get("tdm_delay_model", {})
                require(model.get("model") == "linear-ratio-v1", "evaluator requires declared linear ratio timing")
                key = (route["id"], edge["link"], node, sink)
                require(key in entries, "route hop is unscheduled")
                used.add(key)
                ratio = entries[key].get("tdm_ratio")
                require(isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and
                        math.isfinite(ratio) and ratio >= 1, "invalid concrete TDM ratio")
                delay = model["intercept_ns"] + model["ratio_coefficient_ns"] * ratio
                require(math.isfinite(delay) and delay >= 0, "invalid ratio delay")
                delays[sink] = delays[node] + delay
                pending.append(sink)
        require(len(delays) == len(route["tree_edges"]) + 1, "disconnected route edges")
        require(set(route["sinks"]) <= set(delays), "unreachable route sink")
        by_net[route["net"]] = (route, delays)
    require(used == set(entries), "schedule contains unrelated hops")
    members = {}
    for path in routes["timing"]["paths"]:
        transitions = path.get("cut_transitions")
        require(isinstance(transitions, list) and transitions, "exact sink transitions required")
        require([t["net"] for t in transitions] == path["cut_nets"], "cut transition order differs")
        total = 0.0
        for index, transition in enumerate(transitions):
            route, delays = by_net[transition["net"]]
            require(transition["from"] == route["source"] and transition["to"] in route["sinks"],
                    "timing transition differs from routed demand")
            if index:
                require(transitions[index-1]["to"] == transition["from"], "discontinuous timing transition chain")
            total += delays[transition["to"]]
        required = path.get("required_time_ns", path["clock_period_ns"])
        data = {"transport_ns": total, "clock_domain": path["clock_domain"],
                "clock_period_ns": path["clock_period_ns"], "required_time_ns": required,
                "transitions": transitions}
        for member in path.get("compressed_path_ids", [path["path"]]):
            require(member not in members, "duplicate compressed member")
            members[member] = data
    return members


def summarize_population(originals, assignment, projected, routes, schedule, board, *, original_nets):
    """Join original path constraints to transport without using representative slack."""
    from emuflow.output_realizations import validate_output_realizations
    registry = projected.get("output_realizations")
    require(isinstance(registry, dict), "full original/output population registry required")
    copies = validate_output_realizations(registry)
    require(routes["timing"].get("output_realizations") == registry, "route output registry differs from common input")
    members = member_transport(routes, schedule, board)
    expected_members = {}
    for record in projected["paths"]:
        for member in record.get("compressed_path_ids", [record["id"]]):
            require(member not in expected_members, "duplicate projected member")
            expected_members[member] = record
    require(set(members) == set(expected_members), "routed member population differs from common projection")
    for member, actual in members.items():
        expected = expected_members[member]
        require(actual["transitions"] == expected.get("cut_transitions") and
                actual["clock_domain"] == expected["clock_domain"] and
                close(actual["clock_period_ns"], expected["clock_period_ns"]) and
                close(actual["required_time_ns"], expected["fixed_delay_ns"] + expected["slack_ns"]),
                "routed compressed constraint differs from common projection")
    cuts = {cut["net"]: cut for cut in assignment["cut_nets"]}
    owners = assignment["instance_assignment"]
    by_original = defaultdict(list)
    for copy in copies.values():
        by_original[copy["original_path"]].append(copy)
    seen, consumed = set(), set()
    metrics = {}
    local_count = cross_count = 0
    worst = []
    for path in originals:
        identity = path["id"]
        require(identity not in seen, "duplicate original path")
        seen.add(identity)
        period, fixed, slack = (float(path[key]) for key in ("clock_period_ns", "fixed_delay_ns", "slack_ns"))
        require(all(math.isfinite(x) for x in (period, fixed, slack)) and period > 0 and fixed >= 0,
                "invalid original constraint")
        required = fixed + slack
        clock = path["clock_domain"]
        output = path["endpoint"].get("instance") is None
        candidates = by_original.get(identity, [])
        require(bool(candidates) == output, "original output coverage differs from copy registry")
        transports = []
        if output:
            net = original_nets.get(path["path_nets"][-1])
            require(net is not None, "original output net missing")
            binding = {"port": path["endpoint"]["port"], "bit": path["endpoint"]["bit"]}
            require(len(net["drivers"]) == 1 and any(s.get("instance") is None and
                    s.get("port") == binding["port"] and s.get("bit") == binding["bit"] for s in net["sinks"]),
                    "original output binding differs")
            expected_owners = {owners.get(e["instance"]) for e in net["drivers"] + net["sinks"]
                               if e.get("instance") is not None}
            require(None not in expected_owners and expected_owners == {c["fpga"] for c in candidates},
                    "missing or extra output owner")
        else:
            candidates = [{"id": identity, "fpga": None}]
        for copy in candidates:
            member = copy["id"]
            transitions = path_transitions(path, cuts, owners, copy["fpga"])
            if output:
                require(copy["endpoint"] == binding and copy["terminal_net"] == path["path_nets"][-1] and
                        copy["clock_domain"] == clock and close(copy["clock_period_ns"], period) and
                        close(copy["required_time_ns"], required) and close(copy["fixed_delay_ns"], fixed) and
                        copy["cut_transitions"] == transitions and copy["local"] == (not transitions),
                        "output copy differs from original constraint/ownership")
            if transitions:
                require(member in members, "cross-FPGA original member was omitted")
                data = members[member]
                require(data["transitions"] == transitions and data["clock_domain"] == clock and
                        close(data["clock_period_ns"], period) and close(data["required_time_ns"], required),
                        "transport does not belong to original member")
                transports.append(data["transport_ns"])
                consumed.add(member)
            else:
                require(member not in members, "local member incorrectly projected as crossing")
                transports.append(0.0)
        transport = max(transports)
        final_slack = slack - transport
        local_count += int(transport == 0)
        cross_count += int(transport > 0)
        stat = metrics.setdefault(clock, {"timing_paths": 0, "worst_slack_ns": final_slack,
            "total_negative_slack_ns": 0.0, "negative_slack_paths": 0})
        stat["timing_paths"] += 1
        stat["worst_slack_ns"] = min(stat["worst_slack_ns"], final_slack)
        stat["total_negative_slack_ns"] += min(final_slack, 0.0)
        stat["negative_slack_paths"] += int(final_slack < 0)
        worst.append((final_slack, identity, clock, slack, transport))
        if len(worst) > 200:
            worst = sorted(worst)[:100]
    require(consumed == set(members), "projected members absent from original database")
    require(set(by_original) <= seen, "output original absent from original database")
    population = registry["original_population"]
    identity_digest = population_digest(seen)
    require(len(seen) == population["paths"] and identity_digest == population["path_ids_sha256"],
            "complete original population identity differs")
    require(metrics, "empty original timing population")
    return {"status": "pass", "scope": "whole-original-population-phase5-preplacement-estimate",
            "original_paths": len(seen), "original_path_ids_sha256": identity_digest,
            "local_paths": local_count, "transport_paths": cross_count,
            "worst_slack_ns": min(m["worst_slack_ns"] for m in metrics.values()),
            "total_negative_slack_ns": sum(m["total_negative_slack_ns"] for m in metrics.values()),
            "negative_slack_paths": sum(m["negative_slack_paths"] for m in metrics.values()),
            "per_clock": dict(sorted(metrics.items())),
            "worst_paths": [dict(slack_ns=s, path=p, clock_domain=c, original_slack_ns=o, transport_ns=t)
                            for s,p,c,o,t in sorted(worst)[:100]]}


def summarize_population_artifacts(originals, assignment, projected, routes_path,
                                   schedule_path, board, *, original_nets):
    # Import only after main selects the immutable producer's source root.
    # Managed producers store envelopes; consumers must decode their logical
    # schemas while input seals continue hashing the original file bytes.
    from emuflow.io import read_json
    return summarize_population(originals, assignment, projected,
        read_json(routes_path), read_json(schedule_path), board,
        original_nets=original_nets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-root", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--comparison-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "evaluation output must be new")
    producer = args.producer_root.resolve()
    sys.path[:0] = [str(producer / "src"), str(producer / "scripts")]
    from phase4_nvdla_compare import read, write, digest, verify_contract, stat_identity, runtime_identity
    from emuflow.io import read_json
    from emuflow.phase4 import validate_phase4
    from emuflow.phase5 import validate_phase5
    from emuflow.phase3_storage import read_phase3_cut_timing_view
    from emuflow.sta import iter_sta_path_database_paths
    from emuflow.indexed_physical_ir import IndexedPhysicalIR
    contract = read(args.contract)
    common, platform = Path(contract["common_run"]), Path(contract["platform"])
    verify_contract(contract, common, platform)
    produced = read(args.comparison_root / "comparison.json")
    require(produced.get("status") == "pass" and produced.get("contract_sha256") == digest(args.contract) and
            produced.get("source_sha256") == contract["provenance"]["source_sha256"], "unqualified/mismatched produced pair")
    artifacts = contract["artifacts"]
    assignment_path = common / artifacts["assignment"]
    with sqlite3.connect(f"file:{assignment_path.with_name('assignment.sqlite').resolve()}?mode=ro&immutable=1", uri=True) as db:
        require(db.execute("SELECT 1 FROM copies WHERE is_replica=1 LIMIT 1").fetchone() is None,
                "replicated cell ownership requires a separate qualified evaluator")
    assignment = read_phase3_cut_timing_view(assignment_path, include_instance_assignment=True)
    original_path = common / artifacts["timing_path_database"]
    original = read_json(original_path)
    projected_path = common / artifacts["timing_paths"]
    projected, board = read_json(projected_path), read_json(platform)
    output = {"schema": "emuflow.phase4-full-original-timing/v1", "status": "running",
              "producer_source_sha256": contract["provenance"]["source_sha256"],
              "producer_pair_sha256": digest(args.comparison_root / "comparison.json"),
              "producer_tools": produced.get("tools"),
              "evaluator_sha256": digest(Path(__file__)), "evaluator_runtime": runtime_identity(),
              "evaluator_command": [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
              "common_contract_sha256": digest(args.contract), "arms": {}}
    args.output.mkdir(parents=True)
    files = dict(contract["files"])
    for name in ("A", "B"):
        arm = args.comparison_root / name
        for relative in ("phase4/routes.json", "phase5/schedule.json", "phase5/cross_layer_timing.json"):
            require((arm / relative).is_file(), f"missing complete arm artifact: {name}/{relative}")
        for relative in ("phase4/routes.json", "phase5/schedule.json", "phase5/ratio_plan.json", "phase5/cross_layer_timing.json"):
            path = arm / relative
            if path.is_file():
                files[str(path.resolve())] = digest(path)
    guard = stat_identity(files)
    output["input_sha256"] = files
    try:
        with IndexedPhysicalIR(common / artifacts["ir"]) as ir:
            for name in ("A", "B"):
                arm = args.comparison_root / name
                route_path, schedule_path = arm / "phase4/routes.json", arm / "phase5/schedule.json"
                route_check = validate_phase4(assignment_path, platform, route_path, projected_path)
                ratio = arm / "phase5/ratio_plan.json"
                schedule_check = validate_phase5(route_path, platform, schedule_path,
                    ratio_plan_path=ratio if ratio.is_file() else None, assignment_path=assignment_path)
                require(route_check["status"] == schedule_check["status"] == "pass", "independent legality failed")
                summary = summarize_population_artifacts(iter_sta_path_database_paths(original_path, original, verify_payload=False),
                    assignment, projected, route_path, schedule_path, board, original_nets=ir.nets)
                require(summary["original_paths"] == contract["coverage"]["original_paths"], "original report count differs")
                require(set(summary["per_clock"]) == {"dla_core_clk", "dla_csb_clk"},
                        "full-original clock-domain coverage differs")
                output["arms"][name] = {"full_original": summary,
                    "producer_projected_metrics_diagnostic_only": schedule_check["timing"]}
                write(args.output / "timing.json", output)
        require(stat_identity(files) == guard, "immutable evaluator inputs changed")
        output["status"] = "pass"
        a, b = (output["arms"][name]["full_original"] for name in ("A", "B"))
        output["new_minus_old"] = {k: b[k]-a[k] for k in
            ("worst_slack_ns", "total_negative_slack_ns", "negative_slack_paths")}
        lines = ["Whole-original-population Phase5 preplacement estimate; no physical timing claim.", "",
                 "| Scope / metric | Old Phase4 | Author Phase4 | New minus old |",
                 "|---|---:|---:|---:|"]
        for scope in ("all", "dla_core_clk", "dla_csb_clk"):
            values = [x if scope == "all" else x["per_clock"][scope] for x in (a,b)]
            for key in ("worst_slack_ns", "total_negative_slack_ns", "negative_slack_paths"):
                first, second = (x[key] for x in values)
                lines.append(f"| {scope} {key} | {first} | {second} | {second-first} |")
        lines.extend(["", f"Original paths in each arm: {a['original_paths']}.",
                      "Each original uses its own logic delay; output copies contribute their worst slack once.",
                      "Producer projected metrics are retained only as diagnostics and are not reused as these results."])
        (args.output / "timing.md").write_text("\n".join(lines) + "\n")
    except Exception as error:
        output.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        owner_map = assignment["instance_assignment"]
        if hasattr(owner_map, "connection"):
            owner_map.connection.close()
        write(args.output / "timing.json", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
