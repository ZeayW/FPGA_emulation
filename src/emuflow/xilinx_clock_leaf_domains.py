"""Explicit qualification of physical and declared ideal FF clock leaf loads."""
from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import re
import tempfile

from .errors import ValidationError
from .io import file_sha256
from .xilinx_netlist import open_xilinx_mapped_index


def complete_ideal_ff_clock_leaf_loads(
    audit_path: Path, route_input_path: Path, mapped_path: Path, output_path: Path,
):
    """Seal a separate domain payload including source-declared ideal FF clocks.

    These are capacity reservations, not physically routed boundary clocks.
    The current backend's ideal-boundary-clock contract remains unchanged.
    """
    audit_path, route_input_path, mapped_path, output_path = map(
        Path, (audit_path, route_input_path, mapped_path, output_path)
    )
    if output_path.resolve() == audit_path.resolve():
        raise ValidationError("ideal clock augmentation must publish separately")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    payload = audit.get("placement_domain", {})
    name = payload.get("path")
    if (audit.get("schema") != "emuflow.clock-leaf-domain-audit/v1"
            or audit.get("input_sha256") != file_sha256(route_input_path)
            or audit.get("source", {}).get("mapped_sha256") != file_sha256(mapped_path)
            or not isinstance(name, str) or Path(name).name != name
            or payload.get("schema") != "emuflow.clock-leaf-placement-domain/v1"):
        raise ValidationError("ideal clock leaf augmentation source identity differs")
    source_payload = audit_path.parent / name
    if file_sha256(source_payload) != payload.get("sha256"):
        raise ValidationError("ideal clock leaf augmentation payload hash differs")
    clocks = {}
    capacities = {}
    fixed = defaultdict(set)
    with source_payload.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.split()
            if row[0] == "C":
                key = (row[1], row[2])
                if key in clocks:
                    raise ValidationError("duplicate source clock leaf pin")
                clocks[key] = row[3]
            elif row[0] == "D":
                capacities[int(row[1])] = int(row[2])
            elif row[0] == "P":
                fixed[int(row[1])].add(row[2])
    ideal_nets = set()
    with route_input_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.rstrip("\n").split("\t")
            if (len(row) == 4 and row[:1] == ["EXCLUDED"]
                    and row[2:] == ["boundary_clock", "ideal-boundary-clock"]):
                ideal_nets.add(row[1])
    additions = {}
    with open_xilinx_mapped_index(mapped_path) as mapped:
        modules = mapped["modules"]
        top = next(iter(modules))
        cells = modules[top]["cells"]
        with route_input_path.open(encoding="utf-8") as stream:
            for line in stream:
                row = line.rstrip("\n").split("\t")
                if (row[0] != "CELL" or row[3] not in {"FDRE", "FDCE", "FDSE", "FDPE"}
                        or not row[4].startswith("SLICE_")):
                    continue
                if not re.fullmatch(r"[A-H]FF2?", row[5]):
                    raise ValidationError("unqualified ideal FF BEL")
                key = (row[4], "CLK1" if row[5][0] < "E" else "CLK2")
                if key in clocks:
                    continue
                bit = cells[row[2]]["connections"].get("C")
                if not isinstance(bit, list) or len(bit) != 1 or not isinstance(bit[0], int):
                    raise ValidationError("missing source identity for ideal FF clock")
                clock = f"n{bit[0]}"
                if clock not in ideal_nets:
                    raise ValidationError("unmodeled FF clock is not a declared ideal boundary clock")
                if key in additions and additions[key] != clock:
                    raise ValidationError("ideal FF clock control-set conflict")
                additions[key] = clock
    all_clocks = dict(clocks)
    all_clocks.update(additions)
    relevant_sites = {site for site, _pin in all_clocks}
    domains = {}
    with source_payload.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.split()
            if row[0] == "S" and row[1] in relevant_sites:
                domains[(row[1], "CLK1")] = int(row[2])
                domains[(row[1], "CLK2")] = int(row[3])
    loads = defaultdict(set, {bank: set(values) for bank, values in fixed.items()})
    examples = {}
    for key, clock in all_clocks.items():
        if key not in domains:
            raise ValidationError("FF clock pin has no source-sealed leaf domain")
        bank = domains[key]
        loads[bank].add(clock)
        examples.setdefault(bank, f"IN {key[0]}.{key[1]}")
    overfull = sorted(
        (bank for bank in loads if len(loads[bank]) > capacities[bank]),
        key=lambda bank: (capacities[bank]-len(loads[bank]), bank),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target_payload = output_path.with_name(output_path.name + ".domains.tsv")
    if target_payload.resolve() == source_payload.resolve():
        raise ValidationError("ideal clock augmentation must publish separately")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with source_payload.open(encoding="utf-8") as original:
                for line in original:
                    stream.write(line)
            for (site, pin), clock in sorted(additions.items()):
                stream.write(f"C\t{site}\t{pin}\t{clock}\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, target_payload)
        finally:
            if temporary.exists():
                temporary.unlink()
    result = dict(audit)
    result.update(
        qualification="ordinary-physical-clock-and-declared-ideal-ff-leaf-demand-only",
        oversubscribed_domains=len(overfull),
        maximum_excess_clocks=max((len(loads[bank])-capacities[bank] for bank in overfull), default=0),
        domains=len(loads),
        examples=[dict(domain=bank, clocks=len(loads[bank]), local_capacity=capacities[bank],
                       clock_nets=sorted(loads[bank]), example_pin=examples.get(bank))
                  for bank in overfull[:16]],
        ideal_boundary_ff_clock_nets=sorted({clock for clock in all_clocks.values() if clock in ideal_nets}),
        ideal_boundary_ff_clock_pins=sum(clock in ideal_nets for clock in all_clocks.values()),
        physical_clock_nets=audit["clock_nets"],
        augmentation=dict(source_audit_sha256=file_sha256(audit_path),
                          policy="mapped-ff-clocks-declared-ideal-boundary-only-v1"),
    )
    result["placement_domain"] = dict(payload, path=target_payload.name,
        sha256=file_sha256(target_payload), slice_clock_pins=len(all_clocks),
        ideal_boundary_ff_clock_pins=result["ideal_boundary_ff_clock_pins"])
    from .io import write_json
    write_json(output_path, result, compact=True)
    return result


def add_clock_region_policy(
    audit_path: Path, route_input_path: Path, architecture_path: Path, output_path: Path,
    *, maximum_clocks_per_region: int, physical_handoff_path: Path | None = None,
):
    """Add an explicit region budget to the exact leaf domains for qualification.

    A region budget is a conservative placement policy; exceeding it is not
    itself proof that every possible segmented clock route is impossible.
    """
    from .architecture import ArchitectureDB
    from .io import write_json
    from .xilinx_rwroute import _is_clock_sink_pin

    if (isinstance(maximum_clocks_per_region, bool)
            or not isinstance(maximum_clocks_per_region, int)
            or not 1 <= maximum_clocks_per_region <= 256):
        raise ValidationError("invalid explicit clock region budget")
    audit_path, route_input_path, architecture_path, output_path = map(
        Path, (audit_path, route_input_path, architecture_path, output_path)
    )
    if output_path.resolve() == audit_path.resolve():
        raise ValidationError("clock region policy must publish separately")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    payload = audit.get("placement_domain", {})
    name = payload.get("path")
    if (audit.get("schema") != "emuflow.clock-leaf-domain-audit/v1"
            or audit.get("input_sha256") != file_sha256(route_input_path)
            or payload.get("schema") != "emuflow.clock-leaf-placement-domain/v1"
            or not isinstance(name, str) or not name or Path(name).name != name):
        raise ValidationError("clock region policy source identity differs")
    source_payload = audit_path.parent / name
    if file_sha256(source_payload) != payload.get("sha256"):
        raise ValidationError("clock region policy payload hash differs")
    architecture = ArchitectureDB.load(architecture_path)
    if architecture.value.get("part") != audit.get("source", {}).get("part"):
        raise ValidationError("clock region policy architecture part differs")
    regions_by_site = {
        site["name"]: (site.get("physical_region") or {}).get("clock_region")
        for site in architecture.value["sites"]
    }
    del architecture
    sites = {}
    capacities = {}
    pins = {}
    loads = defaultdict(set)
    with source_payload.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.split()
            if row[0] == "D":
                capacities[int(row[1])] = int(row[2])
            elif row[0] == "S":
                sites[row[1]] = (int(row[2]), int(row[3]))
            elif row[0] == "C":
                pins[(row[1], row[2])] = row[3]
            elif row[0] == "P":
                loads[int(row[1])].add(row[2])
    hard_cells = {}
    physical_clocks = set()
    fixed_region_loads = set()
    route_metadata = {}
    with route_input_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.rstrip("\n").split("\t")
            if row[0] == "META":
                if len(row) != 3 or row[1] in route_metadata:
                    raise ValidationError("invalid regional clock route metadata")
                route_metadata[row[1]] = row[2]
            elif row[0] == "CELL" and row[3] in {"DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"}:
                hard_cells[row[1]] = row[4]
            elif row[0] == "NET" and row[2] == "clock":
                physical_clocks.add(row[1])
            elif (row[0] == "PIN" and row[1] in physical_clocks
                  and row[2] in hard_cells and row[4] == "sink"
                  and _is_clock_sink_pin(row[3])):
                fixed_region_loads.add((hard_cells[row[2]], row[1]))
    if route_metadata != audit.get("source"):
        raise ValidationError("clock region policy route metadata differs")
    required_sites = set(sites) | {site for site, _clock in fixed_region_loads}
    missing = sorted(site for site in required_sites if not regions_by_site.get(site))
    handoff_sha = None
    if physical_handoff_path is not None:
        import sqlite3
        from .xilinx_physical_handoff import validate_xilinx_physical_handoff
        physical_handoff_path = Path(physical_handoff_path)
        checked = validate_xilinx_physical_handoff(physical_handoff_path)
        handoff_sha = checked["physical_handoff_sha256"]
        if any(audit.get("source", {}).get(field) != handoff_sha
               for field in ("packed_sha256", "placement_sha256")):
            raise ValidationError("clock region policy physical handoff identity differs")
        if missing:
            with sqlite3.connect(f"file:{physical_handoff_path.resolve()}?mode=ro&immutable=1", uri=True) as connection:
                placeholders = ",".join("?" for _ in missing)
                rows = connection.execute(
                    "SELECT DISTINCT a.physical_site,c.site FROM assignment a "
                    "JOIN cluster c ON c.ordinal=a.cluster_ordinal "
                    f"WHERE a.physical_site IN ({placeholders})", missing,
                )
                for alias, anchor in rows:
                    region = regions_by_site.get(anchor)
                    if not region:
                        raise ValidationError("physical clock alias anchor has no architecture region")
                    previous = regions_by_site.get(alias)
                    if previous is not None and previous != region:
                        raise ValidationError("physical clock alias maps to different regions")
                    regions_by_site[alias] = region
    unresolved = sorted(site for site in required_sites if not regions_by_site.get(site))
    if unresolved:
        raise ValidationError("physical clock load has no source architecture region: " + ", ".join(unresolved[:8]))
    regions = sorted({regions_by_site[site] for site in required_sites})
    start = max(capacities, default=-1)+1
    region_ids = {region: start+i for i, region in enumerate(regions)}
    for region, domain in region_ids.items():
        capacities[domain] = maximum_clocks_per_region
    examples = {}
    for (site, pin), clock in pins.items():
        leaf = sites[site][0 if pin == "CLK1" else 1]
        region = region_ids[regions_by_site[site]]
        loads[leaf].add(clock)
        loads[region].add(clock)
        examples.setdefault(leaf, f"IN {site}.{pin}")
        examples.setdefault(region, f"IN {site}.{pin}")
    for site, clock in fixed_region_loads:
        domain = region_ids[regions_by_site[site]]
        loads[domain].add(clock)
        examples.setdefault(domain, f"IN {site}.CLK")
    overfull = sorted((domain for domain, clocks in loads.items() if len(clocks)>capacities[domain]),
                      key=lambda domain: (capacities[domain]-len(loads[domain]), domain))
    regional = set(region_ids.values())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target_payload = output_path.with_name(output_path.name + ".domains.tsv")
    if target_payload.resolve() == source_payload.resolve():
        raise ValidationError("clock region policy payload must publish separately")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with source_payload.open(encoding="utf-8") as original:
                for line in original:
                    stream.write(line)
            for region, domain in region_ids.items():
                stream.write(f"D\t{domain}\t{maximum_clocks_per_region}\tregion\n")
            for site in sorted(sites):
                stream.write(f"R\t{site}\t{region_ids[regions_by_site[site]]}\n")
            for site, clock in sorted(fixed_region_loads):
                stream.write(f"P\t{region_ids[regions_by_site[site]]}\t{clock}\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, target_payload)
        finally:
            if temporary.exists():
                temporary.unlink()
    policy = dict(schema="emuflow.clock-region-placement-policy/v1",
                  maximum_clocks_per_region=maximum_clocks_per_region,
                  architecture_sha256=file_sha256(architecture_path),
                  source_audit_sha256=file_sha256(audit_path), region_domains=region_ids,
                  physical_handoff_sha256=handoff_sha)
    result = dict(audit, qualification="ordinary-leaf-and-configured-clock-region-policy",
                  clock_region_policy=policy, oversubscribed_domains=len(overfull),
                  maximum_excess_clocks=max((len(loads[d])-capacities[d] for d in overfull), default=0),
                  over_budget_clock_regions=sum(d in regional for d in overfull),
                  examples=[dict(domain=d, clocks=len(loads[d]), local_capacity=capacities[d],
                                 example_pin=examples.get(d), clock_nets=sorted(loads[d]))
                            for d in overfull[:16]])
    result["placement_domain"] = dict(payload, schema="emuflow.clock-leaf-placement-domain/v2",
        path=target_payload.name, sha256=file_sha256(target_payload),
        region_domains=len(region_ids), fixed_clock_region_loads=len(fixed_region_loads))
    write_json(output_path, result, compact=True)
    return result
