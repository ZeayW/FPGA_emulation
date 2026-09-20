"""Exact site legalization for packed UltraScale+ clusters."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA


XILINX_PLACEMENT_SCHEMA = "emuflow.xilinx-placement/v1"
XILINX_GUIDANCE_SCHEMA = "emuflow.xilinx-global-placement-guidance/v1"
XILINX_CONSTRAINTS_SCHEMA = "emuflow.xilinx-placement-constraints/v1"
XILINX_SINGLE_SLR_PLAN_PROVIDER = "emuflow-xilinx-single-slr-planner-v1"
_SITE_XY_RE = re.compile(r"^(?P<kind>[A-Z0-9_]+)_X(?P<x>\d+)Y(?P<y>\d+)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _physical_site_coordinate(name: str) -> Tuple[str, int, int]:
    match = _SITE_XY_RE.fullmatch(name)
    if match is None:
        raise ValidationError(
            f"site {name!r} does not expose an exact physical X/Y identity"
        )
    return match.group("kind"), int(match.group("x")), int(match.group("y"))


def _materialize_assignment_sites(
    anchor_site: str, assignments: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Expand an Interchange BRAM tile anchor into RapidWright site names."""

    kind, physical_x, physical_y = _physical_site_coordinate(anchor_site)
    result: List[Dict[str, Any]] = []
    for assignment in assignments:
        physical_site = anchor_site
        if kind == "RAMB18":
            bel = assignment.get("bel")
            cell_type = assignment.get("cell_type")
            if cell_type == "RAMB18E2":
                if bel == "RAMB18E2_U":
                    physical_site = f"RAMB18_X{physical_x}Y{physical_y}"
                elif bel == "RAMB18E2_L" and physical_y > 0:
                    physical_site = f"RAMB18_X{physical_x}Y{physical_y - 1}"
                else:
                    raise ValidationError(
                        f"BRAM anchor {anchor_site!r} has invalid RAMB18 BEL {bel!r}"
                    )
            elif cell_type == "RAMB36E2" and bel == "RAMB36E2":
                physical_site = f"RAMB36_X{physical_x}Y{physical_y // 2}"
            else:
                raise ValidationError(
                    f"BRAM anchor {anchor_site!r} cannot materialize "
                    f"{cell_type!r} on {bel!r}"
                )
        result.append({**assignment, "site": physical_site})
    return result


def _load_guidance(
    path: Optional[Path], cluster_ids: Set[str]
) -> Tuple[Dict[str, Tuple[float, float]], Optional[str]]:
    if path is None:
        return {}, None
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != XILINX_GUIDANCE_SCHEMA:
        raise ValidationError("Xilinx placement guidance header is invalid")
    entries = value.get("clusters")
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement guidance clusters are invalid")
    result: Dict[str, Tuple[float, float]] = {}
    for index, entry in enumerate(entries):
        context = f"guidance.clusters[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{context}: expected an object")
        cluster_id = _nonempty(entry.get("cluster"), f"{context}.cluster")
        if cluster_id not in cluster_ids:
            raise ValidationError(f"{context}: unknown cluster {cluster_id!r}")
        if cluster_id in result:
            raise ValidationError(f"{context}: duplicate cluster {cluster_id!r}")
        x, y = entry.get("x"), entry.get("y")
        if (
            isinstance(x, bool) or not isinstance(x, (int, float))
            or isinstance(y, bool) or not isinstance(y, (int, float))
        ):
            raise ValidationError(f"{context}: x/y must be numeric")
        result[cluster_id] = (float(x), float(y))
    return result, _sha256(path)


def _load_constraints(
    path: Optional[Path], cluster_ids: Set[str]
) -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    if path is None:
        return {}, None
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != XILINX_CONSTRAINTS_SCHEMA:
        raise ValidationError("Xilinx placement constraints header is invalid")
    entries = value.get("clusters")
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement constraints clusters are invalid")
    result: Dict[str, Dict[str, str]] = {}
    for index, entry in enumerate(entries):
        context = f"constraints.clusters[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{context}: expected an object")
        cluster_id = _nonempty(entry.get("cluster"), f"{context}.cluster")
        if cluster_id not in cluster_ids:
            raise ValidationError(f"{context}: unknown cluster {cluster_id!r}")
        if cluster_id in result:
            raise ValidationError(f"{context}: duplicate cluster {cluster_id!r}")
        contract = {}
        for key in ("site", "slr", "clock_region"):
            if entry.get(key) is not None:
                contract[key] = _nonempty(entry[key], f"{context}.{key}")
        if not contract:
            raise ValidationError(f"{context}: empty constraint")
        result[cluster_id] = contract
    return result, _sha256(path)


def _template_contracts(
    architecture: ArchitectureDB,
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, List[str]]]:
    templates = architecture.value.get("site_templates", {})
    contracts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for base, template in templates.items():
        modes = [base] + list(template.get("alternative_templates", []))
        bels: Dict[str, Dict[str, Any]] = {}
        for mode in modes:
            for bel in templates[mode]["bels"]:
                existing = bels.get(bel["name"])
                candidate = {**bel, "placement_mode": mode}
                if existing is not None and existing != candidate:
                    raise ValidationError(
                        f"site template {base!r} has ambiguous BEL {bel['name']!r}"
                    )
                bels[bel["name"]] = candidate
        contracts[base] = bels

    sites_by_template: Dict[str, List[str]] = defaultdict(list)
    for site in architecture.value["sites"]:
        base = site.get("template")
        if base is None:
            # Explicit-BEL fixtures are represented by a site-local contract.
            base = f"@site:{site['name']}"
            contracts[base] = {
                bel["name"]: {**bel, "placement_mode": site["type"]}
                for bel in site["bels"]
            }
        sites_by_template[base].append(site["name"])
    for names in sites_by_template.values():
        names.sort(key=_physical_site_coordinate)
    return contracts, dict(sites_by_template)


def _resolve_cluster_bels(
    cluster: Mapping[str, Any], bels: Mapping[str, Mapping[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    declared_modes = set(cluster.get("site_templates", []))
    if not declared_modes:
        raise ValidationError(f"cluster {cluster.get('id')!r} has no site templates")
    resolved = []
    used = set()
    for assignment in cluster.get("assignments", []):
        cell_type = assignment.get("cell_type")
        candidates = assignment.get("bel_candidates")
        if candidates is None:
            candidates = [assignment.get("bel")]
        if (
            not isinstance(candidates, list) or not candidates
            or any(not isinstance(candidate, str) for candidate in candidates)
        ):
            raise ValidationError(
                f"cluster {cluster.get('id')!r} has an invalid BEL contract"
            )
        selected = None
        for candidate_name in sorted(candidates):
            candidate = bels.get(candidate_name)
            if (
                candidate is not None
                and cell_type in candidate.get("compatible_cells", [])
                and candidate.get("placement_mode") in declared_modes
                and candidate_name not in used
            ):
                selected = candidate
                break
        if selected is None:
            return None
        used.add(selected["name"])
        resolved.append({
            "instance": assignment["instance"],
            "cell_type": cell_type,
            "bel": selected["name"],
            "placement_mode": selected["placement_mode"],
        })
    return resolved


def _site_satisfies_constraint(
    site: Mapping[str, Any], constraint: Mapping[str, str]
) -> bool:
    if constraint.get("site") not in {None, site["name"]}:
        return False
    region = site.get("physical_region")
    if not isinstance(region, dict):
        region = {}
    for key in ("slr", "clock_region"):
        if constraint.get(key) is not None and region.get(key) != constraint[key]:
            return False
    return True


def _distance(
    cluster_ids: Sequence[str], sites: Sequence[Mapping[str, Any]],
    guidance: Mapping[str, Tuple[float, float]],
) -> Tuple[float, Tuple[str, ...]]:
    cost = 0.0
    for cluster_id, site in zip(cluster_ids, sites):
        target = guidance.get(cluster_id)
        if target is not None:
            cost += abs(site["x"] - target[0]) + abs(site["y"] - target[1])
    return cost, tuple(site["name"] for site in sites)


def _cascade_cluster_chains(
    packed: Mapping[str, Any], owner: Mapping[str, str]
) -> List[List[str]]:
    chains = []
    occupied: Dict[str, int] = {}
    for chain_index, chain in enumerate(packed.get("cascade_chains", [])):
        clusters = []
        for instance in chain.get("instances", []):
            cluster_id = owner.get(instance)
            if cluster_id is None:
                raise ValidationError("cascade certificate references an unknown cell")
            if not clusters or clusters[-1] != cluster_id:
                clusters.append(cluster_id)
        if len(clusters) <= 1:
            continue
        if len(set(clusters)) != len(clusters):
            raise ValidationError("cascade chain revisits a packed cluster")
        for cluster_id in clusters:
            previous = occupied.get(cluster_id)
            if previous is not None and previous != chain_index:
                raise ValidationError("packed cluster belongs to multiple cascade chains")
            occupied[cluster_id] = chain_index
        chains.append(clusters)
    return chains


def _architecture_slrs(architecture: ArchitectureDB) -> List[str]:
    slrs = {
        region["slr"]
        for site in architecture.value["sites"]
        for region in [site.get("physical_region")]
        if isinstance(region, dict)
        and isinstance(region.get("slr"), str)
        and region["slr"]
    }
    if not slrs:
        raise ValidationError("ArchitectureDB does not expose any physical SLR")
    return sorted(slrs)


def _capacity_flow_feasible(
    cluster_bases: Mapping[str, Sequence[str]],
    capacities: Mapping[str, int],
) -> bool:
    """Solve cluster-to-site-template capacity exactly with a small max-flow."""

    cluster_ids = sorted(cluster_bases)
    bases = sorted(base for base, capacity in capacities.items() if capacity > 0)
    source = 0
    first_cluster = 1
    first_base = first_cluster + len(cluster_ids)
    sink = first_base + len(bases)
    graph: List[List[List[int]]] = [[] for _ in range(sink + 1)]

    def add_edge(left: int, right: int, capacity: int) -> None:
        graph[left].append([right, capacity, len(graph[right])])
        graph[right].append([left, 0, len(graph[left]) - 1])

    base_nodes = {base: first_base + index for index, base in enumerate(bases)}
    for index, cluster_id in enumerate(cluster_ids):
        node = first_cluster + index
        add_edge(source, node, 1)
        for base in sorted(cluster_bases[cluster_id]):
            if base in base_nodes:
                add_edge(node, base_nodes[base], 1)
    for base, node in base_nodes.items():
        add_edge(node, sink, capacities[base])

    flow = 0
    while True:
        levels = [-1] * len(graph)
        levels[source] = 0
        queue = [source]
        for node in queue:
            for right, capacity, _reverse in graph[node]:
                if capacity > 0 and levels[right] < 0:
                    levels[right] = levels[node] + 1
                    queue.append(right)
        if levels[sink] < 0:
            break
        cursors = [0] * len(graph)

        def augment(node: int, available: int) -> int:
            if node == sink:
                return available
            while cursors[node] < len(graph[node]):
                edge = graph[node][cursors[node]]
                right, capacity, reverse = edge
                if capacity > 0 and levels[right] == levels[node] + 1:
                    amount = augment(right, min(available, capacity))
                    if amount:
                        edge[1] -= amount
                        graph[right][reverse][1] += amount
                        return amount
                cursors[node] += 1
            return 0

        while True:
            amount = augment(source, len(cluster_ids) - flow)
            if not amount:
                break
            flow += amount
            if flow == len(cluster_ids):
                return True
    return flow == len(cluster_ids)


def _site_coordinate_rows(
    site_names: Sequence[str],
    sites: Mapping[str, Mapping[str, Any]],
) -> Dict[int, Tuple[int, ...]]:
    rows: Dict[int, List[int]] = defaultdict(list)
    for name in site_names:
        site = sites[name]
        rows[site["y"]].append(site["x"])
    return {y: tuple(sorted(values)) for y, values in rows.items()}


def _nearest_site_lower_bound(
    rows: Mapping[int, Sequence[int]], target: Tuple[float, float]
) -> float:
    target_x, target_y = target
    best: Optional[float] = None
    for y, values in rows.items():
        y_cost = abs(y - target_y)
        if best is not None and y_cost >= best:
            continue
        position = bisect_left(values, target_x)
        x_costs = []
        if position < len(values):
            x_costs.append(abs(values[position] - target_x))
        if position:
            x_costs.append(abs(values[position - 1] - target_x))
        if x_costs:
            cost = y_cost + min(x_costs)
            if best is None or cost < best:
                best = cost
    if best is None:
        raise ValidationError("compatible SLR site inventory is empty")
    return best


def plan_xilinx_single_slr(
    packed_path: Path,
    architecture_path: Path,
    output_path: Path,
    placement_output_path: Path,
    *,
    guidance_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Select one capacity-feasible SLR and materialize its legal placement."""

    packed = read_json(packed_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    clusters = packed.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValidationError("PackedSiteNetlist clusters are invalid or empty")
    cluster_ids = []
    seen = set()
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise ValidationError(f"packed.clusters[{index}]: expected an object")
        cluster_id = _nonempty(cluster.get("id"), f"packed.clusters[{index}].id")
        if cluster_id in seen:
            raise ValidationError(f"duplicate packed cluster {cluster_id!r}")
        seen.add(cluster_id)
        cluster_ids.append(cluster_id)

    architecture = ArchitectureDB.load(architecture_path)
    guidance, guidance_sha = _load_guidance(guidance_path, set(cluster_ids))
    contracts, sites_by_template = _template_contracts(architecture)
    sites = {
        site["name"]: architecture.site_named(site["name"])
        for site in architecture.value["sites"]
    }
    legal_bases = {
        cluster_id: tuple(sorted(
            base for base in sites_by_template
            if _resolve_cluster_bels(cluster, contracts[base]) is not None
        ))
        for cluster_id, cluster in (
            (cluster["id"], cluster) for cluster in clusters
        )
    }
    slrs = _architecture_slrs(architecture)
    sites_by_slr_base: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for base, site_names in sites_by_template.items():
        for site_name in site_names:
            region = sites[site_name].get("physical_region")
            if isinstance(region, dict) and region.get("slr") in slrs:
                sites_by_slr_base[(region["slr"], base)].append(site_name)

    candidate_reports: List[Dict[str, Any]] = []
    ranked: List[Tuple[Tuple[float, float, str], str]] = []
    distance_cache: Dict[
        Tuple[str, Tuple[str, ...]], Dict[int, Tuple[int, ...]]
    ] = {}
    for slr in slrs:
        capacities = {
            base: len(sites_by_slr_base[(slr, base)])
            for base in sites_by_template
        }
        cluster_bases = {
            cluster_id: tuple(
                base for base in legal_bases[cluster_id]
                if capacities[base] > 0
            )
            for cluster_id in cluster_ids
        }
        if (
            any(not bases for bases in cluster_bases.values())
            or not _capacity_flow_feasible(cluster_bases, capacities)
        ):
            candidate_reports.append({
                "slr": slr,
                "status": "capacity-infeasible",
            })
            continue
        distances = []
        if guidance:
            for cluster_id in cluster_ids:
                signature = cluster_bases[cluster_id]
                cache_key = (slr, signature)
                coordinate_rows = distance_cache.get(cache_key)
                if coordinate_rows is None:
                    compatible_sites = sorted({
                        site_name
                        for base in signature
                        for site_name in sites_by_slr_base[(slr, base)]
                    })
                    coordinate_rows = _site_coordinate_rows(
                        compatible_sites, sites
                    )
                    distance_cache[cache_key] = coordinate_rows
                distances.append(_nearest_site_lower_bound(
                    coordinate_rows, guidance[cluster_id]
                ))
        mean = sum(distances) / len(distances) if distances else None
        maximum = max(distances) if distances else None
        candidate_reports.append({
            "slr": slr,
            "status": "capacity-feasible",
            "mean_nearest_site_lower_bound": mean,
            "max_nearest_site_lower_bound": maximum,
        })
        ranked.append(((
            float(mean) if mean is not None else 0.0,
            float(maximum) if maximum is not None else 0.0,
            slr,
        ), slr))

    if not ranked:
        details = "; ".join(
            f"{entry['slr']}: {entry['status']}"
            for entry in candidate_reports
        )
        raise ValidationError(
            "no single SLR has sufficient exact site-template capacity: " + details
        )

    exact_failures = []
    for _rank, selected_slr in sorted(ranked):
        reports = []
        for report in candidate_reports:
            report = dict(report)
            if report["slr"] == selected_slr:
                report["status"] = "selected"
            reports.append(report)
        result = {
            "schema": XILINX_CONSTRAINTS_SCHEMA,
            "status": "pass",
            "provider": XILINX_SINGLE_SLR_PLAN_PROVIDER,
            "part": architecture.part,
            "selected_slr": selected_slr,
            "source": {
                "packed_sha256": _sha256(packed_path),
                "architecture_sha256": _sha256(architecture_path),
                "guidance_sha256": guidance_sha,
            },
            "policy": {
                "scope": "single-slr",
                "capacity_feasibility": "exact-cluster-to-site-template-max-flow",
                "ranking": (
                    "mean-then-max-compatible-nearest-site-lower-bound"
                    if guidance_path is not None else "deterministic-slr-name"
                ),
                "final_feasibility": "exact-site-bel-cascade-legalization",
            },
            "clusters": [
                {"cluster": cluster_id, "slr": selected_slr}
                for cluster_id in sorted(cluster_ids)
            ],
            "candidates": sorted(reports, key=lambda entry: entry["slr"]),
            "summary": {
                "clusters": len(cluster_ids),
                "candidate_slrs": len(candidate_reports),
                "capacity_feasible_slrs": len(ranked),
            },
        }
        write_json(output_path, result, compact=True)
        try:
            placement = place_xilinx_clusters(
                packed_path,
                architecture_path,
                placement_output_path,
                guidance_path=guidance_path,
                constraints_path=output_path,
            )
        except ValidationError as error:
            exact_failures.append(f"{selected_slr}: {error}")
            continue
        result["placement"] = {
            "path": str(placement_output_path),
            "sha256": _sha256(placement_output_path),
            "mean_guidance_displacement": placement["summary"].get(
                "mean_guidance_displacement"
            ),
            "max_guidance_displacement": placement["summary"].get(
                "max_guidance_displacement"
            ),
        }
        # The placement identity is returned in the CLI report rather than
        # written back into the constraints file, whose digest is already bound
        # into that placement certificate.
        return result
    raise ValidationError(
        "capacity-feasible SLRs failed exact cascade legalization: "
        + "; ".join(exact_failures)
    )


def validate_xilinx_single_slr_plan(
    packed_path: Path,
    architecture_path: Path,
    constraints_path: Path,
    placement_path: Path,
    *,
    guidance_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate identity, coverage, and exact feasibility of an SLR plan."""

    packed = read_json(packed_path)
    value = read_json(constraints_path)
    architecture = ArchitectureDB.load(architecture_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    if not isinstance(value, dict) or value.get("schema") != XILINX_CONSTRAINTS_SCHEMA:
        raise ValidationError("Xilinx single-SLR plan header is invalid")
    if value.get("status") != "pass":
        raise ValidationError("Xilinx single-SLR plan status is invalid")
    if value.get("provider") != XILINX_SINGLE_SLR_PLAN_PROVIDER:
        raise ValidationError("Xilinx single-SLR plan provider is invalid")
    if value.get("part") != architecture.part:
        raise ValidationError("Xilinx single-SLR plan part is invalid")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValidationError("Xilinx single-SLR plan source is invalid")
    if source.get("packed_sha256") != _sha256(packed_path):
        raise ValidationError("Xilinx single-SLR plan packed digest is invalid")
    if source.get("architecture_sha256") != _sha256(architecture_path):
        raise ValidationError("Xilinx single-SLR plan architecture digest is invalid")
    cluster_ids = {
        _nonempty(cluster.get("id"), "packed cluster id")
        for cluster in packed.get("clusters", [])
    }
    _guidance, guidance_sha = _load_guidance(guidance_path, cluster_ids)
    if source.get("guidance_sha256") != guidance_sha:
        raise ValidationError("Xilinx single-SLR plan guidance digest is invalid")
    selected_slr = value.get("selected_slr")
    if selected_slr not in _architecture_slrs(architecture):
        raise ValidationError("Xilinx single-SLR plan selected SLR is invalid")
    constraints, _constraints_sha = _load_constraints(constraints_path, cluster_ids)
    if set(constraints) != cluster_ids:
        raise ValidationError("Xilinx single-SLR plan cluster coverage is incomplete")
    if any(contract != {"slr": selected_slr} for contract in constraints.values()):
        raise ValidationError("Xilinx single-SLR plan mixes regions or constraint kinds")
    candidates = value.get("candidates")
    if (
        not isinstance(candidates, list)
        or any(not isinstance(entry, dict) for entry in candidates)
    ):
        raise ValidationError("Xilinx single-SLR plan candidates are invalid")
    candidate_slrs = [entry.get("slr") for entry in candidates]
    if candidate_slrs != _architecture_slrs(architecture):
        raise ValidationError("Xilinx single-SLR plan candidate coverage is invalid")
    selected = [
        entry for entry in candidates
        if entry.get("slr") == selected_slr and entry.get("status") == "selected"
    ]
    if len(selected) != 1:
        raise ValidationError("Xilinx single-SLR plan selected candidate is invalid")
    placement_report = validate_xilinx_placement(
        packed_path,
        architecture_path,
        placement_path,
        constraints_path=constraints_path,
    )
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-single-slr-plan-validation/v1",
        "part": architecture.part,
        "selected_slr": selected_slr,
        "clusters": len(cluster_ids),
        "placement_sha256": placement_report["placement_sha256"],
        "constraints_sha256": _sha256(constraints_path),
    }


def place_xilinx_clusters(
    packed_path: Path,
    architecture_path: Path,
    output_path: Path,
    *,
    guidance_path: Optional[Path] = None,
    constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Place whole packed clusters onto exact, non-overlapping device sites."""

    packed = read_json(packed_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    clusters = packed.get("clusters")
    if not isinstance(clusters, list):
        raise ValidationError("PackedSiteNetlist clusters are invalid")
    cluster_by_id = {}
    owner = {}
    for cluster in clusters:
        cluster_id = _nonempty(cluster.get("id"), "packed cluster id")
        if cluster_id in cluster_by_id:
            raise ValidationError(f"duplicate packed cluster {cluster_id!r}")
        cluster_by_id[cluster_id] = cluster
        for assignment in cluster.get("assignments", []):
            instance = assignment.get("instance")
            if instance in owner:
                raise ValidationError(f"cell {instance!r} belongs to multiple clusters")
            owner[instance] = cluster_id
    cluster_ids = set(cluster_by_id)
    guidance, guidance_sha = _load_guidance(guidance_path, cluster_ids)
    constraints, constraints_sha = _load_constraints(constraints_path, cluster_ids)
    contracts, sites_by_template = _template_contracts(architecture)

    sites = {site["name"]: architecture.site_named(site["name"])
             for site in architecture.value["sites"]}
    site_at_xy = {
        (site["x"], site["y"]): site["name"]
        for site in architecture.value["sites"]
    }
    candidates: Dict[str, List[str]] = {}
    candidate_cache: Dict[
        Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]], List[str]
    ] = {}
    resolved_by_cluster_template: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for cluster_id, cluster in cluster_by_id.items():
        legal_bases = []
        for base, site_names in sites_by_template.items():
            resolved = _resolve_cluster_bels(cluster, contracts[base])
            if resolved is None:
                continue
            resolved_by_cluster_template[(cluster_id, base)] = resolved
            legal_bases.append(base)
        cache_key = (
            tuple(sorted(legal_bases)),
            tuple(sorted(constraints.get(cluster_id, {}).items())),
        )
        legal_sites = candidate_cache.get(cache_key)
        if legal_sites is None:
            legal_sites = sorted({
                site_name
                for base in legal_bases
                for site_name in sites_by_template[base]
                if _site_satisfies_constraint(
                    sites[site_name], constraints.get(cluster_id, {})
                )
            })
            candidate_cache[cache_key] = legal_sites
        if not legal_sites:
            raise ValidationError(
                f"cluster {cluster_id!r} has no legal site after exact constraints"
            )
        candidates[cluster_id] = legal_sites

    site_base = {}
    for base, names in sites_by_template.items():
        for name in names:
            site_base[name] = base

    placed: Dict[str, str] = {}
    used_sites: Set[str] = set()
    chains = _cascade_cluster_chains(packed, owner)
    physical_sites = {
        _physical_site_coordinate(name): name for name in site_base
    }
    membership_cache: Dict[int, Set[str]] = {}

    def candidate_members(cluster_id: str) -> Set[str]:
        values = candidates[cluster_id]
        key = id(values)
        if key not in membership_cache:
            membership_cache[key] = set(values)
        return membership_cache[key]

    # The tightest chain is placed first. Windows are evaluated as a stream;
    # retaining every possible window for a VU19P slice column would duplicate
    # hundreds of thousands of site names in the hot path.
    for chain in sorted(chains, key=lambda item: (len(candidates[item[0]]), item)):
        best = None
        best_cost = None
        for first_name in candidates[chain[0]]:
            kind, physical_x, physical_y = _physical_site_coordinate(first_name)
            names = []
            for offset, cluster_id in enumerate(chain):
                name = physical_sites.get((kind, physical_x, physical_y + offset))
                if (
                    name is None
                    or name not in candidate_members(cluster_id)
                    or name in used_sites
                ):
                    break
                names.append(name)
            if len(names) != len(chain):
                continue
            cost = _distance(chain, [sites[name] for name in names], guidance)
            if best_cost is None or cost < best_cost:
                best = names
                best_cost = cost
                if not any(cluster_id in guidance for cluster_id in chain):
                    break
        if best is None:
            raise ValidationError(
                "dedicated cascade windows conflict for chain: " + " -> ".join(chain)
            )
        for cluster_id, site_name in zip(chain, best):
            placed[cluster_id] = site_name
            used_sites.add(site_name)

    remaining = sorted(
        (cluster_id for cluster_id in cluster_ids if cluster_id not in placed),
        key=lambda cluster_id: (
            len(candidates[cluster_id]),
            -len(cluster_by_id[cluster_id].get("assignments", [])),
            cluster_id,
        ),
    )
    cursors: Dict[int, int] = defaultdict(int)
    row_index_cache: Dict[int, Dict[int, List[Tuple[int, str]]]] = {}

    def nearest_available(
        values: List[str], target: Tuple[float, float]
    ) -> Optional[str]:
        key = id(values)
        rows = row_index_cache.get(key)
        if rows is None:
            mutable_rows: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
            for name in values:
                site = sites[name]
                mutable_rows[site["y"]].append((site["x"], name))
            rows = {
                y: sorted(entries) for y, entries in mutable_rows.items()
            }
            row_index_cache[key] = rows
        target_x, target_y = target
        best_name = None
        best_cost = None
        for y, entries in rows.items():
            y_cost = abs(y - target_y)
            if best_cost is not None and y_cost >= best_cost:
                continue
            position = bisect_left(entries, (target_x, ""))
            left, right = position - 1, position
            while left >= 0 or right < len(entries):
                left_cost = (
                    abs(entries[left][0] - target_x) if left >= 0 else None
                )
                right_cost = (
                    abs(entries[right][0] - target_x)
                    if right < len(entries) else None
                )
                if right_cost is None or (
                    left_cost is not None and left_cost <= right_cost
                ):
                    x_cost, name = left_cost, entries[left][1]
                    left -= 1
                else:
                    x_cost, name = right_cost, entries[right][1]
                    right += 1
                cost = y_cost + x_cost
                if best_cost is not None and cost >= best_cost:
                    break
                if name not in used_sites:
                    best_name, best_cost = name, cost
                    break
        return best_name

    for cluster_id in remaining:
        values = candidates[cluster_id]
        if cluster_id in guidance:
            target_x, target_y = guidance[cluster_id]
            rounded = (int(round(target_x)), int(round(target_y)))
            direct = site_at_xy.get(rounded)
            if (
                direct is not None
                and direct not in used_sites
                and direct in candidate_members(cluster_id)
            ):
                selected = direct
            else:
                selected = nearest_available(values, guidance[cluster_id])
        else:
            key = id(values)
            cursor = cursors[key]
            while cursor < len(values) and values[cursor] in used_sites:
                cursor += 1
            cursors[key] = cursor + 1
            selected = values[cursor] if cursor < len(values) else None
        if selected is None:
            raise ValidationError(
                f"no unoccupied legal site remains for cluster {cluster_id!r}"
            )
        placed[cluster_id] = selected
        used_sites.add(selected)

    placements = []
    displacement = []
    for cluster_id in sorted(cluster_ids):
        site_name = placed[cluster_id]
        site = sites[site_name]
        base = site_base[site_name]
        assignments = _materialize_assignment_sites(
            site_name, resolved_by_cluster_template[(cluster_id, base)]
        )
        target = guidance.get(cluster_id)
        if target is not None:
            displacement.append(
                abs(site["x"] - target[0]) + abs(site["y"] - target[1])
            )
        placements.append({
            "cluster": cluster_id,
            "site": site_name,
            "site_type": site["type"],
            "x": site["x"],
            "y": site["y"],
            "fixed": "site" in constraints.get(cluster_id, {}),
            "physical_region": site.get("physical_region"),
            "assignments": assignments,
        })

    result = {
        "schema": XILINX_PLACEMENT_SCHEMA,
        "status": "pass",
        "part": architecture.part,
        "provider": "emuflow-xilinx-exact-site-legalizer-v1",
        "source": {
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
            "guidance_sha256": guidance_sha,
            "constraints_sha256": constraints_sha,
        },
        "clusters": placements,
        "summary": {
            "clusters": len(placements),
            "cells": sum(len(item["assignments"]) for item in placements),
            "fixed_clusters": sum(item["fixed"] for item in placements),
            "cascade_chains": len(chains),
            "mean_guidance_displacement": (
                sum(displacement) / len(displacement) if displacement else None
            ),
            "max_guidance_displacement": max(displacement) if displacement else None,
            "site_types": dict(sorted(Counter(
                item["site_type"] for item in placements
            ).items())),
        },
    }
    write_json(output_path, result, compact=True)
    validate_xilinx_placement(
        packed_path,
        architecture_path,
        output_path,
        constraints_path=constraints_path,
    )
    return result


def validate_xilinx_placement(
    packed_path: Path,
    architecture_path: Path,
    placement_path: Path,
    *,
    constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Independently re-check cluster ownership, sites, BELs, and cascades."""

    packed = read_json(packed_path)
    placement = read_json(placement_path)
    architecture = ArchitectureDB.load(architecture_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    if not isinstance(placement, dict) or placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
        raise ValidationError("Xilinx placement header is invalid")
    if placement.get("status") != "pass" or placement.get("part") != architecture.part:
        raise ValidationError("Xilinx placement identity is invalid")
    source = placement.get("source", {})
    if source.get("packed_sha256") != _sha256(packed_path):
        raise ValidationError("Xilinx placement packed digest is invalid")
    if source.get("architecture_sha256") != _sha256(architecture_path):
        raise ValidationError("Xilinx placement architecture digest is invalid")

    cluster_by_id = {cluster["id"]: cluster for cluster in packed.get("clusters", [])}
    constraints, constraints_sha = _load_constraints(
        constraints_path, set(cluster_by_id)
    )
    if source.get("constraints_sha256") != constraints_sha:
        raise ValidationError("Xilinx placement constraint digest is invalid")
    contracts, _sites_by_template = _template_contracts(architecture)
    site_base = {}
    for site in architecture.value["sites"]:
        base = site.get("template", f"@site:{site['name']}")
        site_base[site["name"]] = base

    entries = placement.get("clusters")
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement clusters are invalid")
    placed = {}
    occupied = set()
    cell_owners = {}
    for index, entry in enumerate(entries):
        context = f"placement.clusters[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{context}: expected an object")
        cluster_id = entry.get("cluster")
        cluster = cluster_by_id.get(cluster_id)
        if cluster is None or cluster_id in placed:
            raise ValidationError(f"{context}: unknown or duplicate cluster")
        site_name = entry.get("site")
        site = architecture.site_named(site_name) if isinstance(site_name, str) else None
        if site is None:
            raise ValidationError(f"{context}: unknown site")
        if site_name in occupied:
            raise ValidationError(f"{context}: physical site overlap")
        occupied.add(site_name)
        if (entry.get("x"), entry.get("y")) != (site["x"], site["y"]):
            raise ValidationError(f"{context}: coordinates do not match site")
        if not _site_satisfies_constraint(site, constraints.get(cluster_id, {})):
            raise ValidationError(f"{context}: placement constraint is violated")
        resolved = _resolve_cluster_bels(cluster, contracts[site_base[site_name]])
        if resolved is not None:
            resolved = _materialize_assignment_sites(site_name, resolved)
        if resolved is None or entry.get("assignments") != resolved:
            raise ValidationError(f"{context}: exact BEL assignment is invalid")
        for assignment in resolved:
            instance = assignment["instance"]
            if instance in cell_owners:
                raise ValidationError(f"cell {instance!r} is placed more than once")
            cell_owners[instance] = cluster_id
        placed[cluster_id] = site_name
    if set(placed) != set(cluster_by_id):
        raise ValidationError("Xilinx placement cluster ownership is incomplete")

    packed_cells = {
        assignment["instance"]
        for cluster in cluster_by_id.values()
        for assignment in cluster.get("assignments", [])
    }
    if set(cell_owners) != packed_cells:
        raise ValidationError("Xilinx placement cell ownership is incomplete")
    owner = {instance: cluster_id for instance, cluster_id in cell_owners.items()}
    chains = _cascade_cluster_chains(packed, owner)
    for chain in chains:
        coordinates = [_physical_site_coordinate(placed[cluster]) for cluster in chain]
        first_kind, first_x, first_y = coordinates[0]
        expected = [
            (first_kind, first_x, first_y + offset)
            for offset in range(len(chain))
        ]
        if coordinates != expected:
            raise ValidationError(
                "dedicated cascade placement is not physically contiguous: "
                + " -> ".join(chain)
            )
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-placement-validation/v1",
        "clusters": len(placed),
        "cells": len(cell_owners),
        "cascade_chains": len(chains),
        "placement_sha256": _sha256(placement_path),
    }
